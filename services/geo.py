import math
from typing import Optional

EARTH_RADIUS_KM = 6371.0088


def distance_km(
    lat1: float,
    lon1: float,
    lat2: Optional[float],
    lon2: Optional[float],
) -> Optional[float]:
    if lat2 is None or lon2 is None:
        return None

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return EARTH_RADIUS_KM * c
