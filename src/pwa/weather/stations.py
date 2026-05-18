"""City → (lat, lon, tz, resolution_station_hint) mapping.

Polymarket daily-temperature markets always specify a resolution station in the
description (e.g. "LaGuardia Airport Station" for NYC, "Heathrow" for London).
The Open-Meteo grid is fine enough that the city centroid is close to the
station; we use the station's actual coordinates when known.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Station:
    city_key: str
    display_name: str
    lat: float
    lon: float
    tz: str
    resolution_station: str


_STATIONS: dict[str, Station] = {}


def _add(s: Station) -> None:
    _STATIONS[s.city_key] = s


_add(Station("nyc", "New York City (LaGuardia)", 40.7772, -73.8726, "America/New_York", "LaGuardia Airport"))
_add(Station("new-york-city", "New York City (LaGuardia)", 40.7772, -73.8726, "America/New_York", "LaGuardia Airport"))
_add(Station("miami", "Miami International Airport", 25.7959, -80.2870, "America/New_York", "Miami International Airport"))
_add(Station("los-angeles", "Los Angeles International Airport", 33.9416, -118.4085, "America/Los_Angeles", "LAX"))
_add(Station("la", "Los Angeles International Airport", 33.9416, -118.4085, "America/Los_Angeles", "LAX"))
_add(Station("chicago", "Chicago O'Hare", 41.9786, -87.9048, "America/Chicago", "O'Hare International Airport"))
_add(Station("denver", "Denver International Airport", 39.8617, -104.6731, "America/Denver", "Denver International Airport"))
_add(Station("san-francisco", "San Francisco International", 37.6213, -122.3790, "America/Los_Angeles", "SFO"))
_add(Station("sf", "San Francisco International", 37.6213, -122.3790, "America/Los_Angeles", "SFO"))
_add(Station("dallas", "Dallas-Fort Worth", 32.8998, -97.0403, "America/Chicago", "DFW"))
_add(Station("philadelphia", "Philadelphia International Airport", 39.8729, -75.2437, "America/New_York", "PHL"))
_add(Station("phoenix", "Phoenix Sky Harbor", 33.4373, -112.0078, "America/Phoenix", "Phoenix Sky Harbor"))
_add(Station("boston", "Boston Logan", 42.3656, -71.0096, "America/New_York", "Boston Logan"))
_add(Station("seattle", "Seattle-Tacoma International", 47.4502, -122.3088, "America/Los_Angeles", "Sea-Tac"))
_add(Station("atlanta", "Atlanta Hartsfield-Jackson", 33.6407, -84.4277, "America/New_York", "ATL"))
_add(Station("houston", "Houston George Bush", 29.9902, -95.3368, "America/Chicago", "IAH"))
_add(Station("london", "London Heathrow", 51.4700, -0.4543, "Europe/London", "Heathrow"))
_add(Station("paris", "Paris Charles de Gaulle", 49.0097, 2.5479, "Europe/Paris", "Charles de Gaulle"))
_add(Station("berlin", "Berlin Brandenburg", 52.3667, 13.5033, "Europe/Berlin", "BER"))
_add(Station("madrid", "Madrid Barajas", 40.4983, -3.5676, "Europe/Madrid", "MAD"))
_add(Station("rome", "Rome Fiumicino", 41.8003, 12.2389, "Europe/Rome", "FCO"))
_add(Station("moscow", "Moscow Sheremetyevo", 55.9726, 37.4146, "Europe/Moscow", "Sheremetyevo"))
_add(Station("istanbul", "Istanbul Airport", 41.2753, 28.7519, "Europe/Istanbul", "IST"))
_add(Station("dubai", "Dubai International Airport", 25.2532, 55.3657, "Asia/Dubai", "DXB"))
_add(Station("tokyo", "Tokyo Haneda", 35.5494, 139.7798, "Asia/Tokyo", "Haneda"))
_add(Station("seoul", "Seoul Incheon", 37.4602, 126.4407, "Asia/Seoul", "Incheon"))
_add(Station("beijing", "Beijing Capital", 40.0799, 116.6031, "Asia/Shanghai", "PEK"))
_add(Station("shanghai", "Shanghai Pudong", 31.1443, 121.8083, "Asia/Shanghai", "PVG"))
_add(Station("hong-kong", "Hong Kong International", 22.3080, 113.9185, "Asia/Hong_Kong", "HKG"))
_add(Station("singapore", "Singapore Changi", 1.3644, 103.9915, "Asia/Singapore", "Changi"))
_add(Station("taipei", "Taipei Taoyuan", 25.0777, 121.2328, "Asia/Taipei", "TPE"))
_add(Station("mumbai", "Mumbai Chhatrapati Shivaji", 19.0896, 72.8656, "Asia/Kolkata", "BOM"))
_add(Station("delhi", "Delhi Indira Gandhi", 28.5562, 77.1000, "Asia/Kolkata", "DEL"))
_add(Station("tel-aviv", "Tel Aviv Ben Gurion", 32.0114, 34.8866, "Asia/Jerusalem", "TLV"))
_add(Station("sydney", "Sydney Kingsford Smith", -33.9399, 151.1753, "Australia/Sydney", "SYD"))
_add(Station("melbourne", "Melbourne Tullamarine", -37.6690, 144.8410, "Australia/Melbourne", "MEL"))
_add(Station("sao-paulo", "São Paulo Guarulhos", -23.4356, -46.4731, "America/Sao_Paulo", "GRU"))
_add(Station("rio-de-janeiro", "Rio de Janeiro Galeão", -22.8089, -43.2436, "America/Sao_Paulo", "GIG"))
_add(Station("mexico-city", "Mexico City Benito Juárez", 19.4361, -99.0719, "America/Mexico_City", "MEX"))
_add(Station("toronto", "Toronto Pearson", 43.6777, -79.6248, "America/Toronto", "YYZ"))
_add(Station("vancouver", "Vancouver International", 49.1967, -123.1815, "America/Vancouver", "YVR"))


def get_station(city_key: str) -> Station | None:
    return _STATIONS.get(city_key.lower())


def known_cities() -> list[str]:
    return sorted(_STATIONS.keys())
