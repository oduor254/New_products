# ── Fix: PostgreSQL sets REQUESTS_CA_BUNDLE to its own (wrong) path,
#    which breaks every HTTPS call made by gspread / google-auth.
#    Override it here before any network library is imported.
import os, certifi, json
from dotenv import load_dotenv
load_dotenv()  # loads .env in local dev; no-op on Vercel
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
os.environ["SSL_CERT_FILE"]      = certifi.where()

from flask import Flask, jsonify, send_from_directory, request
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
from collections import Counter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=BASE_DIR)

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION  ← edit these values to match your spreadsheet
# ─────────────────────────────────────────────────────────────────────────────
# Local dev fallback — ignored on Vercel (use GOOGLE_CREDENTIALS_JSON env var instead)
JSON_KEY_PATH  = r"C:\Users\Oduor\Downloads\JSON Files\retention-484110-9e4520124486.json"

SPREADSHEET_ID = "1QOlKAXwkKoD-neLG2MUWh6aUHcNnbnwWYhhhVa1rhcs"
WORKSHEET_NAME = "Shops"

# Only these products are considered "new" — all other rows are excluded
NEW_PRODUCTS = {
    "MEGA", "PRIME", "MINI UMBRA", "CATHY HANDBAG", "PIONEER",
    "CLAIRE HANDBAG", "SIERRA HANDBAG", "MONAH BP", "TAJI", "COSMO",
    "LOOP BP", "SPARK", "LEGACY", "SKYE HB", "NALA", "ARM BAND",
    "CESS", "IMANI", "MANDY HB", "CHASE", "VOYAGE", "CELINE SLING BAG",
    "AMORA", "MONTANA", "SPLASH BACKPACK",
}
# ─────────────────────────────────────────────────────────────────────────────


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

def _get_credentials():
    """Return credentials from env var (Vercel) or local JSON file (dev)."""
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return Credentials.from_service_account_file(JSON_KEY_PATH, scopes=SCOPES)


def get_dataframe(retries: int = 3):
    last_err = None
    for attempt in range(retries):
        try:
            creds  = _get_credentials()
            client = gspread.authorize(creds)
            sheet  = client.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
            data   = sheet.get_all_records()
            break
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                import time; time.sleep(2 ** attempt)  # exponential back-off
            else:
                raise last_err

    df = pd.DataFrame(data)

    # --- Normalise column names (strip whitespace) ---
    df.columns = df.columns.str.strip()

    # --- Date parsing (support mixed day/month order) ---
    if "Date" in df.columns:
        # Let pandas infer formats (defaults to month-first which matches the sheet's MM/DD/YYYY format)
        df["Date"] = pd.to_datetime(df["Date"], dayfirst=False, errors="coerce")

    # --- Numeric price ---
    if "Price" in df.columns:
        df["Price"] = pd.to_numeric(df["Price"], errors="coerce")

    # --- Strip string columns ---
    for col in ["Gender", "Color", "Location", "Product", "Category"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().str.title()

    # --- Keep only new products ---
    if "Product" in df.columns:
        new_products_title = {p.title() for p in NEW_PRODUCTS}
        df = df[df["Product"].isin(new_products_title)].reset_index(drop=True)

    return df


# ─── KEY METRICS ──────────────────────────────────────────────────────────────
@app.route("/api/key-metrics")
def key_metrics():
    df = get_dataframe()

    total        = len(df)
    female       = int((df["Gender"].str.lower() == "female").sum())
    male         = int((df["Gender"].str.lower() == "male").sum())
    female_pct   = round((female / total * 100), 1) if total else 0
    male_pct     = round((male   / total * 100), 1) if total else 0
    unique_locs  = int(df["Location"].nunique()) if "Location" in df.columns else 0

    total_revenue = round(float(df["Price"].sum()), 2) if "Price" in df.columns else 0
    avg_price     = round(float(df["Price"].mean()), 2) if "Price" in df.columns else 0

    # Date range
    if "Date" in df.columns and df["Date"].notna().any():
        min_date = df["Date"].min().strftime("%d %b %Y")
        max_date = df["Date"].max().strftime("%d %b %Y")
        date_range = f"{min_date} – {max_date}"
    else:
        date_range = "N/A"

    # Top product
    top_product = (
        df["Product"].value_counts().idxmax()
        if "Product" in df.columns and not df["Product"].empty
        else "N/A"
    )

    # Top category
    top_category = (
        df["Category"].value_counts().idxmax()
        if "Category" in df.columns and not df["Category"].empty
        else "N/A"
    )

    return jsonify({
        "total_customers" : total,
        "female_customers": female,
        "male_customers"  : male,
        "female_pct"      : female_pct,
        "male_pct"        : male_pct,
        "unique_locations": unique_locs,
        "date_range"      : date_range,
        "total_revenue"   : total_revenue,
        "avg_price"       : avg_price,
        "top_product"     : top_product,
        "top_category"    : top_category,
    })


# ─── COLOR ANALYSIS ───────────────────────────────────────────────────────────
@app.route("/api/color-analysis")
def color_analysis():
    df    = get_dataframe()
    total = len(df)
    counts = df["Color"].value_counts().reset_index()
    counts.columns = ["color", "count"]
    counts["percentage"] = (counts["count"] / total * 100).round(1)
    return jsonify(counts.to_dict(orient="records"))


# ─── LOCATION ANALYSIS ────────────────────────────────────────────────────────
@app.route("/api/location-analysis")
def location_analysis():
    df    = get_dataframe()
    total = len(df)
    counts = df["Location"].value_counts().reset_index()
    counts.columns = ["shop", "count"]
    counts["percentage"] = (counts["count"] / total * 100).round(1)

    # Revenue per location
    if "Price" in df.columns:
        rev = df.groupby("Location")["Price"].sum().reset_index()
        rev.columns = ["shop", "revenue"]
        counts = counts.merge(rev, on="shop", how="left")
        counts["revenue"] = counts["revenue"].round(2)

    return jsonify(counts.to_dict(orient="records"))


# ─── WEEKLY SALES TREND ───────────────────────────────────────────────────────
@app.route("/api/weekly-trend")
def weekly_trend():
    df = get_dataframe()
    if "Date" not in df.columns or df["Date"].isna().all():
        return jsonify([])

    # Drop rows with unparseable dates before period conversion
    df = df.dropna(subset=["Date"]).copy()
    # Create an actual datetime column for chronological grouping
    df["Week_Start"] = df["Date"].dt.to_period("W").dt.start_time

    # Use whichever name/count column exists
    count_col = next((c for c in ["First Name", "Name", "Customer"] if c in df.columns), df.columns[0])
    weekly = df.groupby("Week_Start").agg(
        count=(count_col, "count"),
        revenue=("Price", "sum"),
    ).reset_index()
    
    # Sort chronologically, then create the string label
    weekly = weekly.sort_values("Week_Start")
    weekly["Week"] = weekly["Week_Start"].dt.strftime("%d %b '%y")
    weekly = weekly.drop(columns=["Week_Start"])
    
    weekly["revenue"] = weekly["revenue"].round(2)
    return jsonify(weekly.to_dict(orient="records"))


# ─── PRODUCT ANALYSIS ─────────────────────────────────────────────────────────
@app.route("/api/product-analysis")
def product_analysis():
    df    = get_dataframe()
    total = len(df)
    counts = df["Product"].value_counts().reset_index()
    counts.columns = ["product", "count"]
    counts["percentage"] = (counts["count"] / total * 100).round(1)

    if "Price" in df.columns:
        rev = df.groupby("Product")["Price"].agg(["sum", "mean"]).reset_index()
        rev.columns = ["product", "total_revenue", "avg_price"]
        rev["total_revenue"] = rev["total_revenue"].round(2)
        rev["avg_price"]     = rev["avg_price"].round(2)
        counts = counts.merge(rev, on="product", how="left")

    return jsonify(counts.to_dict(orient="records"))


# ─── BAGS PERFORMANCE ─────────────────────────────────────────────────────────
@app.route("/api/bags-performance")
def bags_performance():
    df = get_dataframe()

    # Since the vast majority of products are bags (even if not explicitly named 'bag'),
    # we treat all valid product rows as bag-related for this breakdown.
    bags_df = df.copy()

    if bags_df.empty:
        return jsonify({"message": "No bag products found", "data": []})

    total_bags      = int(len(bags_df))
    bag_revenue     = round(float(bags_df["Price"].sum()), 2) if "Price" in bags_df.columns else 0
    avg_bag_price   = round(float(bags_df["Price"].mean()), 2) if "Price" in bags_df.columns else 0

    # Gender split for bags
    female_bags = int((bags_df["Gender"].str.lower() == "female").sum())
    male_bags   = int((bags_df["Gender"].str.lower() == "male").sum())

    # Popular bag colors
    bag_colors = bags_df["Color"].value_counts().head(5).reset_index()
    bag_colors.columns = ["color", "count"]

    # Popular bag locations
    bag_locs = bags_df["Location"].value_counts().head(5).reset_index()
    bag_locs.columns = ["location", "count"]

    # Bag names breakdown
    bag_names = bags_df["Product"].value_counts().reset_index()
    bag_names.columns = ["product", "count"]
    bag_names["percentage"] = (bag_names["count"] / total_bags * 100).round(1)

    # Monthly trend for bags
    if "Month-Year" in bags_df.columns:
        monthly = bags_df.groupby("Month-Year").size().reset_index(name="count")
    else:
        monthly = []

    return jsonify({
        "total_bags"   : total_bags,
        "bag_revenue"  : bag_revenue,
        "avg_bag_price": avg_bag_price,
        "female_buyers": female_bags,
        "male_buyers"  : male_bags,
        "top_colors"   : bag_colors.to_dict(orient="records"),
        "top_locations": bag_locs.to_dict(orient="records"),
        "bag_names"    : bag_names.to_dict(orient="records"),
        "monthly_trend": monthly if isinstance(monthly, list) else monthly.to_dict(orient="records"),
    })


# ─── CATEGORY ANALYSIS ────────────────────────────────────────────────────────
@app.route("/api/category-analysis")
def category_analysis():
    df    = get_dataframe()
    total = len(df)
    counts = df["Category"].value_counts().reset_index()
    counts.columns = ["category", "count"]
    counts["percentage"] = (counts["count"] / total * 100).round(1)

    if "Price" in df.columns:
        rev = df.groupby("Category")["Price"].sum().reset_index()
        rev.columns = ["category", "revenue"]
        rev["revenue"] = rev["revenue"].round(2)
        counts = counts.merge(rev, on="category", how="left")

    return jsonify(counts.to_dict(orient="records"))


# ─── MONTHLY TREND ────────────────────────────────────────────────────────────
@app.route("/api/monthly-trend")
def monthly_trend():
    df = get_dataframe()
    if "Month-Year" not in df.columns:
        return jsonify([])
    monthly = df.groupby("Month-Year").agg(
        count=("First Name", "count"),
        revenue=("Price", "sum"),
    ).reset_index()
    monthly.columns = ["month_year", "count", "revenue"]
    monthly["revenue"] = monthly["revenue"].round(2)
    return jsonify(monthly.to_dict(orient="records"))


# ─── GENDER BY PRODUCT ────────────────────────────────────────────────────────
@app.route("/api/gender-by-product")
def gender_by_product():
    df = get_dataframe()
    pivot = (
        df.groupby(["Product", "Gender"])
        .size()
        .unstack(fill_value=0)
        .reset_index()
    )
    return jsonify(pivot.to_dict(orient="records"))


# ─── PRODUCT LIST (for filter dropdown) ──────────────────────────────────────
@app.route("/api/product-list")
def product_list():
    df = get_dataframe()
    products = sorted(df["Product"].dropna().unique().tolist())
    return jsonify(products)


# ─── BAG / PRODUCT FILTER ─────────────────────────────────────────────────────
@app.route("/api/bag-filter")
def bag_filter():
    product = request.args.get("product", "").strip()
    start   = request.args.get("start", "").strip()
    end     = request.args.get("end", "").strip()

    df = get_dataframe()

    # Apply product filter
    if product:
        df = df[df["Product"].str.lower() == product.lower()]

    # Apply date range filter
    if start:
        df = df[df["Date"] >= pd.to_datetime(start, errors="coerce")]
    if end:
        df = df[df["Date"] <= pd.to_datetime(end, errors="coerce")]

    if df.empty:
        return jsonify({"message": "No data found for the selected filters."})

    total     = int(len(df))
    revenue   = round(float(df["Price"].sum()), 2)  if "Price" in df.columns else 0
    avg_price = round(float(df["Price"].mean()), 2) if "Price" in df.columns else 0
    female    = int((df["Gender"].str.lower() == "female").sum())
    male      = int((df["Gender"].str.lower() == "male").sum())

    # Top colors
    top_colors = []
    if "Color" in df.columns:
        c = df["Color"].value_counts().head(5).reset_index()
        c.columns = ["color", "count"]
        top_colors = c.to_dict(orient="records")

    # Top locations
    top_locations = []
    if "Location" in df.columns:
        l = df["Location"].value_counts().head(5).reset_index()
        l.columns = ["location", "count"]
        top_locations = l.to_dict(orient="records")

    # Weekly trend
    weekly_trend = []
    if "Date" in df.columns and df["Date"].notna().any():
        d = df.dropna(subset=["Date"]).copy()
        d["Week_Start"] = d["Date"].dt.to_period("W").dt.start_time
        count_col = next((c for c in ["First Name", "Name", "Customer"] if c in d.columns), d.columns[0])
        w = d.groupby("Week_Start").agg(
            count=(count_col, "count"),
            revenue=("Price", "sum"),
        ).reset_index()
        w = w.sort_values("Week_Start")
        w["Week"] = w["Week_Start"].dt.strftime("%d %b '%y")
        w = w.drop(columns=["Week_Start"])
        w["revenue"] = w["revenue"].round(2)
        weekly_trend = w.to_dict(orient="records")

    return jsonify({
        "product"       : product or "All Products",
        "total"         : total,
        "revenue"       : revenue,
        "avg_price"     : avg_price,
        "female_buyers" : female,
        "male_buyers"   : male,
        "top_colors"    : top_colors,
        "top_locations" : top_locations,
        "weekly_trend"  : weekly_trend,
    })


# ─── SERVE DASHBOARD ────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return send_from_directory(BASE_DIR, "dashboard.html")


if __name__ == "__main__":
    print("\n  ✅  Dashboard running →  http://127.0.0.1:5005\n")
    app.run(debug=True, port=5005)