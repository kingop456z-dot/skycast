import sqlite3
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, g, redirect, render_template, request, url_for

app = Flask(__name__)
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
        "hourly": "precipitation_probability",
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

    return {
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
