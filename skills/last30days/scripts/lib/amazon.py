"""Amazon product + customer-review search via Canopy API for /last30days.

The painpoint angle: given a niche (e.g. "standing desk for small apartments"),
find the top-ranked Amazon products for that search, then pull each product's
most-helpful customer reviews. Real buyers describing what's missing, broken, or
disappointing *is* the pain-point signal — it tells us the gaps in what's on the
market so we can build/sell something that closes them.

Two REST calls per product run:
  1. GET /api/amazon/search?searchTerm=<niche>        -> top products (ranked)
  2. GET /api/amazon/product/reviews?asin=<asin>      -> most-helpful reviews

Each review becomes one normalized item (one piece of evidence). Reviews are
treated as *evergreen*: a complaint from eight months ago is still a valid pain
point, so the item carries no published_at (the real review date is kept in
metadata) and therefore survives the recency window filter regardless of how the
caller set --days.

Requires CANOPY_API_KEY (https://rest.canopyapi.co). 100 free requests/month,
then pay-as-you-go. Auth header: `API-KEY`.
"""

from typing import Any, Dict, List, Optional

from . import http, log
from .relevance import token_overlap_relevance as _compute_relevance

CANOPY_BASE = "https://rest.canopyapi.co/api/amazon"

# How many products to inspect and how many reviews to pull per product.
# Cost is ~ (1 search + N product-review calls) Canopy credits per run.
DEPTH_CONFIG = {
    "quick":   {"products": 3, "reviews_per_product": 5},
    "default": {"products": 5, "reviews_per_product": 8},
    "deep":    {"products": 8, "reviews_per_product": 10},
}

# Floor so a genuine pain-point review isn't pruned just because it doesn't echo
# the query verbatim — the product was a top hit for the niche, so its reviews
# are on-topic by construction. Mirrors the GitHub project-mode floor in signals.
_RELEVANCE_FLOOR = 0.35


def _log(msg: str) -> None:
    log.source_log("Amazon", msg)


def _extract_core_subject(topic: str) -> str:
    """Reduce a verbose niche to the core product subject for the search box."""
    from .query import extract_core_subject
    _AMAZON_NOISE = frozenset({
        'best', 'top', 'good', 'great', 'cheap', 'budget', 'affordable',
        'review', 'reviews', 'recommendation', 'recommendations',
        'latest', 'new', 'trending', 'popular',
        'buy', 'buying', 'guide', 'vs', 'comparison',
    })
    return extract_core_subject(topic, noise=_AMAZON_NOISE)


def canopy_headers(token: str) -> Dict[str, str]:
    """Build Canopy request headers (API-KEY + JSON content type)."""
    return {"API-KEY": token, "Content-Type": "application/json"}


def _first(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present, non-empty value among `keys` (Canopy's JSON
    shape varies across endpoints, so we look up several aliases)."""
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
    return default


def _dig(data: Any, *path: str) -> Any:
    """Walk a nested dict by key path, returning None if any hop is missing."""
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _as_dict_list(value: Any) -> List[Dict[str, Any]]:
    return value if isinstance(value, list) and (not value or isinstance(value[0], dict)) else []


def search_products(
    topic: str, token: str, count: int
) -> List[Dict[str, Any]]:
    """Search Amazon and return up to `count` top-ranked organic products.

    Canopy's `domain`/`sort` query params currently 500 the endpoint, so we send
    only `searchTerm`. Results come back in Amazon's own ranking order.
    """
    core = _extract_core_subject(topic)
    _log(f"Searching Amazon for '{core}' (top {count} products)")
    try:
        data = http.get(
            f"{CANOPY_BASE}/search",
            params={"searchTerm": core},
            headers=canopy_headers(token),
            timeout=30,
            retries=2,
        )
    except Exception as e:  # noqa: BLE001 — tiered source: degrade, never crash run
        _log(f"Canopy search error: {e}")
        return []

    # Real shape: data.amazonProductSearchResults.productResults.results[]
    results = _as_dict_list(
        _dig(data, "data", "amazonProductSearchResults", "productResults", "results")
    )
    parsed: List[Dict[str, Any]] = []
    for p in results:
        if len(parsed) >= count:
            break
        asin = _first(p, "asin", "ASIN")
        if not asin or p.get("sponsored"):  # skip ads; we want organic top sellers
            continue
        parsed.append({
            "asin": str(asin),
            "title": str(_first(p, "title", "productTitle", "name", default="")),
            "url": str(_first(p, "url", "link", default=f"https://www.amazon.com/dp/{asin}")),
            "rating": _first(p, "rating", "stars"),
            "reviews_total": _first(p, "ratingsTotal", "reviewsTotal", "reviewCount", "totalReviews"),
        })
    _log(f"Found {len(parsed)} products")
    return parsed


def fetch_reviews(
    asin: str, token: str, count: int
) -> List[Dict[str, Any]]:
    """Fetch the most-helpful reviews for one product (by ASIN).

    `topReviews` is already Amazon's most-helpful set; we only send `asin`
    (Canopy's `domain`/`sort` params currently 500 the endpoint).
    """
    try:
        data = http.get(
            f"{CANOPY_BASE}/product/reviews",
            params={"asin": asin},
            headers=canopy_headers(token),
            timeout=30,
            retries=2,
        )
    except Exception as e:  # noqa: BLE001
        _log(f"Canopy reviews error for {asin}: {e}")
        return []
    # Real shape: data.amazonProduct.topReviews[] (fallback: reviewsPaginated.reviews)
    reviews = _as_dict_list(_dig(data, "data", "amazonProduct", "topReviews"))
    if not reviews:
        reviews = _as_dict_list(
            _dig(data, "data", "amazonProduct", "reviewsPaginated", "reviews")
        )
    # Most-helpful first (defensive: re-sort by helpful votes if the API didn't).
    parsed = [
        {
            "id": str(_first(r, "id", "reviewId", default="")),
            "title": str(_first(r, "title", "heading", default="")),
            "body": str(_first(r, "body", "text", "review", "content", default="")),
            "rating": _first(r, "rating", "stars"),
            "helpful_votes": int(_first(r, "helpfulVotes", "helpful_votes", "helpfulCount", default=0) or 0),
            "verified": bool(_first(r, "verifiedPurchase", "verified", default=False)),
            "author": str(_first((r.get("reviewer") or {}) if isinstance(r.get("reviewer"), dict) else {},
                                  "name", default="") or _first(r, "reviewerName", "author", default="")),
            "date": _first(r, "date", "reviewDate", "reviewedAt"),
        }
        for r in reviews
        if isinstance(r, dict)
    ]
    parsed = [r for r in parsed if r["body"]]
    parsed.sort(key=lambda r: r["helpful_votes"], reverse=True)
    return parsed[:count]


def search_amazon(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    token: str = None,
) -> Dict[str, Any]:
    """Search Amazon products + reviews for a niche.

    Args:
        topic: the niche / search query
        from_date, to_date: accepted for interface parity; reviews are evergreen
            and not date-filtered (see module docstring).
        depth: 'quick' | 'default' | 'deep'
        token: Canopy API key

    Returns:
        {"items": [...]} — one item per review, ready for normalization.
    """
    if not token:
        return {"items": [], "error": "No CANOPY_API_KEY configured"}

    cfg = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    core = _extract_core_subject(topic)
    products = search_products(topic, token, cfg["products"])

    items: List[Dict[str, Any]] = []
    for product in products:
        reviews = fetch_reviews(product["asin"], token, cfg["reviews_per_product"])
        for rev in reviews:
            # Include the product name in the title so the evidence reads as
            # "<Product> — <complaint>" and carries the niche keywords for
            # relevance scoring.
            review_title = rev["title"] or (rev["body"][:80])
            full_title = f"{product['title']} — {review_title}" if product["title"] else review_title
            relevance = _compute_relevance(core, f"{product['title']} {rev['body']}", [])
            relevance = max(relevance, _RELEVANCE_FLOOR)
            stars = rev["rating"]
            star_txt = f"{stars}★ " if stars is not None else ""
            items.append({
                "id": rev["id"] or f"{product['asin']}-{len(items) + 1}",
                "title": full_title,
                "body": rev["body"],
                "url": product["url"],
                "author": rev["author"],
                "product": product["title"],
                # Evergreen: leave date out of published_at so the window filter
                # keeps it; preserve the real date in metadata.
                "review_date": rev["date"],
                "engagement": {"helpful_votes": rev["helpful_votes"]},
                "relevance": relevance,
                "why_relevant": f"Amazon review ({star_txt}helpful={rev['helpful_votes']}) of {product['title'][:50]}",
                "metadata": {
                    "asin": product["asin"],
                    "product_title": product["title"],
                    "product_rating": product["rating"],
                    "product_reviews_total": product["reviews_total"],
                    "review_rating": stars,
                    "verified_purchase": rev["verified"],
                    "review_date": rev["date"],
                },
            })

    _log(f"Collected {len(items)} reviews across {len(products)} products")
    return {"items": items}


def parse_amazon_response(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract the item list from a search_amazon() result."""
    return result.get("items", [])
