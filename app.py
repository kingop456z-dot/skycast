import os
import sqlite3
from datetime import datetime
from pathlib import Path

import requests
from markupsafe import Markup
from flask import Flask, g, redirect, render_template, request, url_for

app = Flask(__name__)
# Vercel's filesystem is read-only except /tmp (temporary storage).
if os.environ.get("VERCEL"):
    DB_PATH = Path("/tmp/skycast.db")
else:
    DB_PATH = Path(__file__).with_name("skycast.db")

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

ALIASES = {
    "banglore": "Bengaluru", "bangalore": "Bengaluru", "bengalore": "Bengaluru", "blr": "Bengaluru",
    "bombay": "Mumbai", "madras": "Chennai", "calcutta": "Kolkata", "gurgaon": "Gurugram",
    "poona": "Pune", "trivandrum": "Thiruvananthapuram", "baroda": "Vadodara", "mysore": "Mysuru",
    "mangalore": "Mangaluru", "cochin": "Kochi", "benares": "Varanasi", "banaras": "Varanasi",
    "pondicherry": "Puducherry", "vizag": "Visakhapatnam", "hubli": "Hubballi", "belgaum": "Belagavi",
    "allahabad": "Prayagraj", "delhi": "New Delhi", "nyc": "New York", "la": "Los Angeles",
}

QUICK = [
    {"name": "Bengaluru", "country": "India", "lat": 12.97, "lon": 77.59},
    {"name": "Mumbai", "country": "India", "lat": 19.08, "lon": 72.88},
    {"name": "New Delhi", "country": "India", "lat": 28.61, "lon": 77.21},
    {"name": "Chennai", "country": "India", "lat": 13.08, "lon": 80.27},
    {"name": "Kolkata", "country": "India", "lat": 22.57, "lon": 88.36},
    {"name": "Hyderabad", "country": "India", "lat": 17.39, "lon": 78.49},
    {"name": "Pune", "country": "India", "lat": 18.52, "lon": 73.86},
    {"name": "Ahmedabad", "country": "India", "lat": 23.02, "lon": 72.57},
]

DEFAULT_CITY = {"name": "New Delhi", "country": "India", "admin1": "", "lat": 28.61, "lon": 77.21}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            create table if not exists recent_searches (
                id integer primary key autoincrement,
                name text not null,
                country text,
                admin1 text,
                latitude real not null,
                longitude real not null,
                created_at text not null default current_timestamp
            )
            """
        )


def describe(code):
    if code == 0:
        return "Clear sky", "☀️"
    if code <= 2:
        return "Partly cloudy", "⛅"
    if code == 3:
        return "Overcast", "☁️"
    if code <= 48:
        return "Fog", "🌫️"
    if code <= 57:
        return "Drizzle", "🌦️"
    if code <= 67:
        return "Rain", "🌧️"
    if code <= 77:
        return "Snow", "❄️"
    if code <= 82:
        return "Rain showers", "🌧️"
    if code <= 86:
        return "Snow showers", "🌨️"
    return "Thunderstorm", "⛈️"


def is_rainy(code):
    return 51 <= code <= 67 or 80 <= code <= 82 or code >= 95


def geocode(name):
    res = requests.get(GEO_URL, params={"name": name, "count": 8}, timeout=10)
    res.raise_for_status()
    return res.json().get("results") or []


def find_cities(term):
    name = ALIASES.get(term.lower(), term)
    results = geocode(name)
    size = len(name) - 1
    while not results and size >= 4:
        results = geocode(name[:size])
        size -= 1
    return results


def fetch_forecast(lat, lon):
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
        "hourly": "precipitation_probability,temperature_2m",
        "minutely_15": "precipitation,rain,showers",
        "forecast_minutely_15": 12,
        "models": "best_match",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum",
        "timezone": "auto",
        "forecast_days": 7,
    }
    res = requests.get(FORECAST_URL, params=params, timeout=10)
    res.raise_for_status()
    return res.json()



# ---------------------------------------------------------------- charts (server-rendered SVG)
def _scale(v, lo, hi, a, b):
    return a + (v - lo) / ((hi - lo) or 1) * (b - a)


def _chart_frame(w, h, pad, lo, hi, ticks=4, unit="\u00b0"):
    """Grid lines and y-axis labels. Returns (svg_parts, y_fn)."""
    l, r, t, b = pad
    y = lambda v: _scale(v, lo, hi, h - b, t)
    parts = []
    for i in range(ticks + 1):
        v = lo + (hi - lo) * i / ticks
        parts.append(f'<line x1="{l}" x2="{w - r}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="gl"/>')
        parts.append(f'<text x="{l - 8}" y="{y(v) + 4:.1f}" class="ax" text-anchor="end">{round(v)}{unit}</text>')
    return parts, y


def hourly_chart(hours, temps, rain):
    """Line chart of temperature for the next hours, with rain-chance bars underneath."""
    w, h = 720, 280
    pad = (46, 16, 28, 58)
    lo, hi = min(temps) - 2, max(temps) + 2
    parts, y = _chart_frame(w, h, pad, lo, hi)
    n = len(temps)
    x = lambda i: _scale(i, 0, n - 1, pad[0], w - pad[1])
    # rain chance bars (0-100%) in the bottom strip
    bw = (w - pad[0] - pad[1]) / n * 0.55
    for i, p in enumerate(rain):
        bh = max(p / 100 * 26, 1)
        parts.append(f'<rect x="{x(i) - bw / 2:.1f}" y="{h - 22 - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="2" class="rb"><title>{hours[i]} rain {p}%</title></rect>')
    pts = " ".join(f"{x(i):.1f},{y(t):.1f}" for i, t in enumerate(temps))
    area = f"{x(0):.1f},{h - pad[3]} {pts} {x(n - 1):.1f},{h - pad[3]}"
    parts.append(f'<polygon points="{area}" class="area"/>')
    parts.append(f'<polyline points="{pts}" class="line"/>')
    for i in range(0, n, 3):
        parts.append(f'<text x="{x(i):.1f}" y="{h - 4}" class="ax" text-anchor="middle">{hours[i]}</text>')
    for i, t in enumerate(temps):
        parts.append(f'<circle cx="{x(i):.1f}" cy="{y(t):.1f}" r="3" class="dot"><title>{hours[i]}: {round(t)}\u00b0C, rain {rain[i]}%</title></circle>')
    hi_i, lo_i = temps.index(max(temps)), temps.index(min(temps))
    for i, kind in ((hi_i, "max"), (lo_i, "min")):
        dy = -10 if kind == "max" else 18
        parts.append(f'<text x="{x(i):.1f}" y="{y(temps[i]) + dy:.1f}" class="pk" text-anchor="middle">{round(temps[i])}\u00b0</text>')
    return Markup(f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="Temperature for the next {n} hours">{"".join(parts)}</svg>')


def weekly_chart(labels, highs, lows):
    """High and low temperature lines for the week, with the range shaded between them."""
    w, h = 720, 280
    pad = (46, 16, 28, 34)
    lo, hi = min(lows) - 2, max(highs) + 2
    parts, y = _chart_frame(w, h, pad, lo, hi)
    n = len(highs)
    x = lambda i: _scale(i, 0, n - 1, pad[0] + 20, w - pad[1] - 20)
    top = [(x(i), y(v)) for i, v in enumerate(highs)]
    bot = [(x(i), y(v)) for i, v in enumerate(lows)]
    band = " ".join(f"{a:.1f},{b:.1f}" for a, b in top + bot[::-1])
    parts.append(f'<polygon points="{band}" class="band"/>')
    parts.append('<polyline points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in top) + '" class="hi"/>')
    parts.append('<polyline points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in bot) + '" class="lo"/>')
    for i in range(n):
        parts.append(f'<circle cx="{top[i][0]:.1f}" cy="{top[i][1]:.1f}" r="3.5" class="hd"><title>{labels[i]}: high {round(highs[i])}\u00b0C</title></circle>')
        parts.append(f'<circle cx="{bot[i][0]:.1f}" cy="{bot[i][1]:.1f}" r="3.5" class="ld"><title>{labels[i]}: low {round(lows[i])}\u00b0C</title></circle>')
        parts.append(f'<text x="{top[i][0]:.1f}" y="{top[i][1] - 9:.1f}" class="pk" text-anchor="middle">{round(highs[i])}\u00b0</text>')
        parts.append(f'<text x="{bot[i][0]:.1f}" y="{bot[i][1] + 17:.1f}" class="pk lowt" text-anchor="middle">{round(lows[i])}\u00b0</text>')
        parts.append(f'<text x="{x(i):.1f}" y="{h - 6}" class="ax" text-anchor="middle">{labels[i]}</text>')
    return Markup(f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="7-day high and low temperature">{"".join(parts)}</svg>')


def build_weather(data):
    cur = data["current"]
    times = data["hourly"]["time"]
    hour = cur["time"][:13]
    idx = next((i for i, t in enumerate(times) if t >= hour), 0)
    chance = data["hourly"]["precipitation_probability"][idx] or 0
    upcoming = [v or 0 for v in (data.get("minutely_15") or {}).get("precipitation") or []]
    soon = next((i for i, v in enumerate(upcoming) if v > 0.1), -1)
    raining = is_rainy(cur["weather_code"]) or cur["precipitation"] > 0 or (upcoming and upcoming[0] > 0.1)

    if raining:
        status = "☔ It's raining now"
    elif soon >= 0:
        status = f"☔ Rain expected in ~{soon * 15} min"
    elif chance >= 50:
        status = "☔ Rain likely soon"
    else:
        status = "🌂 No rain expected right now"

    label, icon = describe(cur["weather_code"])
    daily = data["daily"]
    days = []
    for i, day in enumerate(daily["time"]):
        days.append({
            "label": "Today" if i == 0 else datetime.strptime(day, "%Y-%m-%d").strftime("%a"),
            "icon": describe(daily["weather_code"][i])[1],
            "rain": daily["precipitation_probability_max"][i],
            "low": round(daily["temperature_2m_min"][i]),
            "high": round(daily["temperature_2m_max"][i]),
        })

    htimes = data["hourly"]["time"][idx:idx + 24]
    htemps = [v for v in data["hourly"].get("temperature_2m", [])[idx:idx + 24] if v is not None]
    hrain = [v or 0 for v in data["hourly"]["precipitation_probability"][idx:idx + 24]]
    hours = [t[11:16] for t in htimes]
    n = min(len(hours), len(htemps), len(hrain))
    hourly_svg = hourly_chart(hours[:n], htemps[:n], hrain[:n]) if n >= 2 else None
    weekly_svg = weekly_chart(
        [d["label"] for d in days],
        [daily["temperature_2m_max"][i] for i in range(len(days))],
        [daily["temperature_2m_min"][i] for i in range(len(days))],
    ) if len(days) >= 2 else None

    return {
        "hourly_chart": hourly_svg,
        "weekly_chart": weekly_svg,
        "temp": round(cur["temperature_2m"]),
        "feels": round(cur["apparent_temperature"]),
        "humidity": cur["relative_humidity_2m"],
        "wind": round(cur["wind_speed_10m"]),
        "precip": cur["precipitation"],
        "label": label,
        "icon": icon,
        "chance": chance,
        "status": status,
        "wet": bool(raining or soon >= 0 or chance >= 50),
        "today_rain": daily["precipitation_probability_max"][0],
        "local_time": cur["time"][11:],
        "days": days,
    }


def recent_searches(limit=8):
    rows = get_db().execute(
        "select name, country, admin1, latitude, longitude from recent_searches order by id desc limit 40"
    ).fetchall()
    seen = set()
    out = []
    for row in rows:
        key = (row["name"], row["country"])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": row["name"],
            "country": row["country"] or "",
            "admin1": row["admin1"] or "",
            "lat": row["latitude"],
            "lon": row["longitude"],
        })
    return out[:limit]


def city_from_args(args):
    try:
        lat = float(args["lat"])
        lon = float(args["lon"])
    except (KeyError, ValueError):
        return dict(DEFAULT_CITY)
    return {
        "name": args.get("name") or "Unknown",
        "country": args.get("country", ""),
        "admin1": args.get("admin1", ""),
        "lat": lat,
        "lon": lon,
    }


@app.route("/")
def index():
    term = request.args.get("q", "").strip()
    city = city_from_args(request.args)
    results = []
    search_error = None
    weather = None
    weather_error = None

    if len(term) > 1:
        try:
            results = find_cities(term)
        except requests.RequestException:
            search_error = "Search isn't working right now. Try again."

    try:
        weather = build_weather(fetch_forecast(city["lat"], city["lon"]))
    except (requests.RequestException, KeyError, IndexError, TypeError):
        weather_error = "Couldn't load weather. Try again."

    return render_template(
        "index.html",
        term=term,
        results=results,
        search_error=search_error,
        city=city,
        weather=weather,
        weather_error=weather_error,
        quick=QUICK,
        recent=recent_searches(),
    )


@app.route("/pick", methods=["POST"])
def pick():
    form = request.form
    try:
        lat = float(form["lat"])
        lon = float(form["lon"])
    except (KeyError, ValueError):
        return redirect(url_for("index"))

    name = form.get("name", "").strip()[:120] or "Unknown"
    country = form.get("country", "").strip()[:120]
    admin1 = form.get("admin1", "").strip()[:120]

    db = get_db()
    db.execute(
        "insert into recent_searches (name, country, admin1, latitude, longitude) values (?, ?, ?, ?, ?)",
        (name, country or None, admin1 or None, lat, lon),
    )
    db.commit()
    return redirect(url_for("index", name=name, country=country, admin1=admin1, lat=lat, lon=lon))


init_db()

def free_port(start=5000):
    import socket
    for port in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


if __name__ == "__main__":
    port = free_port()
    print(f"\n  SkyCast running -> open http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=True)
