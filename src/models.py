from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Restaurant:
    name: str
    address: str = ""
    city: str = ""
    country: str = ""
    google_maps_url: str = ""
    latitude: float | None = None
    longitude: float | None = None
    # How much to trust the coordinates when rejecting far-away partner listings:
    # "exact" (user input / Google Maps place URL), "approx" (geocoded POI or
    # address), "coarse" (city centre only, distance gate disabled), "none".
    coords_accuracy: str = "none"

    @property
    def has_coords(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def search_text(self) -> str:
        return " ".join(p for p in (self.name, self.address, self.city) if p)

    def label(self) -> str:
        return f"{self.name} ({self.city})" if self.city else self.name


@dataclass(slots=True)
class MenuItem:
    name: str
    price: float | None = None
    currency: str | None = None
    category: str = ""
    description: str = ""
    image_url: str = ""


@dataclass(slots=True)
class MenuResult:
    source: str  # "foodpanda" | "ubereats" | "google_maps"
    source_url: str
    matched_name: str
    match_score: float
    items: list[MenuItem] = field(default_factory=list)
