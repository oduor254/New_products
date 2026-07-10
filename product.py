# ── Fix: PostgreSQL sets REQUESTS_CA_BUNDLE to its own (wrong) path,
#    which breaks every HTTPS call made by gspread / google-auth.
#    Override it here before any network library is imported.
import os, certifi, json, time
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

# Allow overriding via env vars (Vercel → Settings → Environment Variables)
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1QOlKAXwkKoD-neLG2MUWh6aUHcNnbnwWYhhhVa1rhcs")
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "Shops")

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
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return Credentials.from_service_account_file(JSON_KEY_PATH, scopes=SCOPES)


# ── In-memory cache (survives warm Vercel invocations; TTL = 5 min) ──────────
_cache: dict = {"df": None, "ts": 0.0}
CACHE_TTL = 300  # seconds


def _fetch_fresh() -> pd.DataFrame:
    """Fetch from Google Sheets and return a cleaned DataFrame."""
    last_err = None
    for attempt in range(3):
        try:
            creds  = _get_credentials()
            client = gspread.authorize(creds)
            sheet  = client.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
            data   = sheet.get_all_records()
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                raise last_err

    df = pd.DataFrame(data)
    df.columns = df.columns.str.strip()

    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], dayfirst=False, errors="coerce")

    for col in ["Price", "Quantity", "Total"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ["Gender", "Color", "Location", "Product", "Category"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().str.title()

    if "Product" in df.columns:
        new_products_title = {p.title() for p in NEW_PRODUCTS}
        df = df[df["Product"].isin(new_products_title)].reset_index(drop=True)

    return df


def get_dataframe() -> pd.DataFrame:
    """Return cached DataFrame, refreshing from Google Sheets if TTL expired."""
    if _cache["df"] is None or (time.time() - _cache["ts"]) > CACHE_TTL:
        _cache["df"] = _fetch_fresh()
        _cache["ts"] = time.time()
    return _cache["df"]


# ─── CONSOLIDATED DASHBOARD DATA (single Google Sheets fetch) ────────────────
@app.route("/api/dashboard-data")
def dashboard_data():
    df    = get_dataframe()
    total = len(df)
    if total == 0:
        return jsonify({"error": "No data found for the new products in the sheet."}), 404

    # ── Key metrics ──────────────────────────────────────────────────────────
    female      = int((df["Gender"].str.lower() == "female").sum())
    male        = int((df["Gender"].str.lower() == "male").sum())
    metrics = {
        "total_customers" : total,
        "female_customers": female,
        "male_customers"  : male,
        "female_pct"      : round(female / total * 100, 1) if total else 0,
        "male_pct"        : round(male   / total * 100, 1) if total else 0,
        "unique_locations": int(df["Location"].nunique()) if "Location" in df.columns else 0,
        "total_revenue"   : round(float(df["Total"].sum()), 2)  if "Total"    in df.columns else 0,
        "total_units"     : int(df["Quantity"].sum())            if "Quantity" in df.columns else total,
        "avg_price"       : round(float(df["Price"].mean()), 2) if "Price"    in df.columns else 0,
        "date_range"      : (
            df["Date"].min().strftime("%d %b %Y") + " – " + df["Date"].max().strftime("%d %b %Y")
            if "Date" in df.columns and df["Date"].notna().any() else "N/A"
        ),
        "top_product"  : df["Product"].value_counts().idxmax()  if "Product"  in df.columns and total else "N/A",
        "top_category" : df["Category"].value_counts().idxmax() if "Category" in df.columns and total else "N/A",
    }

    # ── Colors ───────────────────────────────────────────────────────────────
    color_counts = df["Color"].value_counts().reset_index()
    color_counts.columns = ["color", "count"]
    color_counts["percentage"] = (color_counts["count"] / total * 100).round(1)

    # ── Locations ────────────────────────────────────────────────────────────
    loc_counts = df["Location"].value_counts().reset_index()
    loc_counts.columns = ["shop", "count"]
    loc_counts["percentage"] = (loc_counts["count"] / total * 100).round(1)
    if "Total" in df.columns:
        rev = df.groupby("Location")["Total"].sum().reset_index()
        rev.columns = ["shop", "revenue"]
        loc_counts = loc_counts.merge(rev, on="shop", how="left")
        loc_counts["revenue"] = loc_counts["revenue"].round(2)

    # ── Weekly trend ─────────────────────────────────────────────────────────
    weekly = []
    if "Date" in df.columns and df["Date"].notna().any():
        dw = df.dropna(subset=["Date"]).copy()
        dw["Week_Start"] = dw["Date"].dt.to_period("W").dt.start_time
        count_col = next((c for c in ["First Name", "Name", "Customer"] if c in dw.columns), dw.columns[0])
        wk = dw.groupby("Week_Start").agg(count=(count_col, "count"), revenue=("Total", "sum")).reset_index()
        wk = wk.sort_values("Week_Start")
        wk["Week"] = wk["Week_Start"].dt.strftime("%d %b '%y")
        wk["revenue"] = wk["revenue"].round(2)
        weekly = wk.drop(columns=["Week_Start"]).to_dict(orient="records")

    # ── Products ─────────────────────────────────────────────────────────────
    prod_counts = df["Product"].value_counts().reset_index()
    prod_counts.columns = ["product", "count"]
    prod_counts["percentage"] = (prod_counts["count"] / total * 100).round(1)
    if "Total" in df.columns:
        pr = df.groupby("Product").agg(total_revenue=("Total", "sum"), avg_price=("Price", "mean")).reset_index()
        pr["total_revenue"] = pr["total_revenue"].round(2)
        pr["avg_price"]     = pr["avg_price"].round(2)
        prod_counts = prod_counts.merge(pr, on="product", how="left")

    # ── Categories ───────────────────────────────────────────────────────────
    cat_counts = df["Category"].value_counts().reset_index()
    cat_counts.columns = ["category", "count"]
    cat_counts["percentage"] = (cat_counts["count"] / total * 100).round(1)
    if "Total" in df.columns:
        cr = df.groupby("Category")["Total"].sum().reset_index()
        cr.columns = ["category", "revenue"]
        cr["revenue"] = cr["revenue"].round(2)
        cat_counts = cat_counts.merge(cr, on="category", how="left")

    # ── Monthly trend ────────────────────────────────────────────────────────
    monthly = []
    if "Month-Year" in df.columns:
        mn = df.groupby("Month-Year").agg(count=("First Name", "count"), revenue=("Total", "sum")).reset_index()
        mn.columns = ["month_year", "count", "revenue"]
        mn["revenue"] = mn["revenue"].round(2)
        monthly = mn.to_dict(orient="records")

    # ── Bags performance ─────────────────────────────────────────────────────
    bag_total   = total
    bag_revenue = round(float(df["Total"].sum()), 2) if "Total" in df.columns else 0
    bag_colors  = df["Color"].value_counts().head(5).reset_index()
    bag_colors.columns = ["color", "count"]
    bag_locs    = df["Location"].value_counts().head(5).reset_index()
    bag_locs.columns = ["location", "count"]
    bag_names   = df["Product"].value_counts().reset_index()
    bag_names.columns = ["product", "count"]
    bag_names["percentage"] = (bag_names["count"] / bag_total * 100).round(1)

    # ── Product list (for filter dropdown) ───────────────────────────────────
    products = sorted(df["Product"].dropna().unique().tolist())

    return jsonify({
        "metrics"   : metrics,
        "colors"    : color_counts.to_dict(orient="records"),
        "locations" : loc_counts.to_dict(orient="records"),
        "weekly"    : weekly,
        "products"  : prod_counts.to_dict(orient="records"),
        "categories": cat_counts.to_dict(orient="records"),
        "monthly"   : monthly,
        "bags": {
            "total_bags"   : bag_total,
            "bag_revenue"  : bag_revenue,
            "avg_bag_price": round(float(df["Price"].mean()), 2) if "Price" in df.columns else 0,
            "female_buyers": int((df["Gender"].str.lower() == "female").sum()),
            "male_buyers"  : int((df["Gender"].str.lower() == "male").sum()),
            "top_colors"   : bag_colors.to_dict(orient="records"),
            "top_locations": bag_locs.to_dict(orient="records"),
            "bag_names"    : bag_names.to_dict(orient="records"),
        },
        "product_list": products,
    })


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
    revenue   = round(float(df["Total"].sum()), 2)  if "Total"    in df.columns else 0
    avg_price = round(float(df["Price"].mean()), 2) if "Price"    in df.columns else 0
    units     = int(df["Quantity"].sum())            if "Quantity" in df.columns else total
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
            revenue=("Total", "sum"),
        ).reset_index()
        w = w.sort_values("Week_Start")
        w["Week"] = w["Week_Start"].dt.strftime("%d %b '%y")
        w = w.drop(columns=["Week_Start"])
        w["revenue"] = w["revenue"].round(2)
        weekly_trend = w.to_dict(orient="records")

    return jsonify({
        "product"       : product or "All Products",
        "total"         : total,
        "units"         : units,
        "revenue"       : revenue,
        "avg_price"     : avg_price,
        "female_buyers" : female,
        "male_buyers"   : male,
        "top_colors"    : top_colors,
        "top_locations" : top_locations,
        "weekly_trend"  : weekly_trend,
    })


# ─── HEALTH CHECK ────────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    import sys
    status = {"python": sys.version, "ok": False}

    creds_env = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    status["creds_source"] = "env_var" if creds_env else "local_file"

    if not creds_env:
        status["error"] = f"GOOGLE_CREDENTIALS_JSON env var not set — falling back to local file: {JSON_KEY_PATH}"
        local_exists = os.path.exists(JSON_KEY_PATH)
        status["local_file_exists"] = local_exists
        if not local_exists:
            return jsonify(status), 500

    try:
        creds  = _get_credentials()
        client = gspread.authorize(creds)
        sheet  = client.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
        status["sheet_rows"] = len(sheet.get_all_values())
        status["ok"] = True
    except Exception as e:
        status["error"] = str(e)
        return jsonify(status), 500

    return jsonify(status)


# ─── SERVE DASHBOARD ────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return send_from_directory(BASE_DIR, "dashboard.html")


if __name__ == "__main__":
    print("\n  ✅  Dashboard running →  http://127.0.0.1:5005\n")
    app.run(debug=True, port=5005)