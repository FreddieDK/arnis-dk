"""Requests for Dataforsyningen and Datafordeler through the MCDK cache (geodata/mcdk-cache).

The cache answers http://<cache>/<host>/<path>?<query> as https://<host>/<path>?<query>
answers and adds the token or apiKey itself, so a client changes only the URL:

    import mcdk_cache
    url = mcdk_cache.url('https://api.dataforsyningen.dk/dhm_wcs_DAF?service=WCS&...&token=...')
    # MCDK_CACHE_URL=http://10.0.40.40:
    #   http://10.0.40.40/api.dataforsyningen.dk/dhm_wcs_DAF?service=WCS&...   (no token)

Without MCDK_CACHE_URL, url() returns its argument unchanged, key and all, so the same
code runs with and without the cache. Copy this file next to the code that uses it; it
needs only the standard library.
"""
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

# The hosts the cache serves; the same list as CACHED_HOSTS in the gateway.
HOSTS = frozenset({'api.dataforsyningen.dk', 'skraafoto-cdn.dataforsyningen.dk',
                   'api.datafordeler.dk', 'graphql.datafordeler.dk'})
SECRET_PARAMS = frozenset({'token', 'apikey'})


def base(env=None):
    """The cache's address from MCDK_CACHE_URL, without a trailing slash; '' when unset."""
    return (os.environ if env is None else env).get('MCDK_CACHE_URL', '').strip().rstrip('/')


def enabled(env=None):
    return bool(base(env))


def url(upstream, cache=None):
    """`upstream` through the cache, without token/apiKey. Unchanged when no cache is set,
    or when the host is not one the cache serves."""
    cache = base() if cache is None else cache.strip().rstrip('/')
    parts = urlsplit(upstream)
    host = (parts.hostname or '').lower()
    if not cache or parts.scheme != 'https' or host not in HOSTS or parts.port not in (None, 443):
        return upstream
    query = '&'.join(part for part in parts.query.split('&')
                     if part and part.split('=', 1)[0].lower() not in SECRET_PARAMS)
    return f'{cache}/{host}{parts.path or "/"}' + (f'?{query}' if query else '')


def upstream(cached):
    """The other way: a URL the cache handed out (http://<cache>/<host>/<path>, as in its
    STAC answers) as https://<host>/<path>, so what a client keeps does not depend on
    whether the cache was on. Any other URL is returned unchanged."""
    parts = urlsplit(cached)
    host, _, rest = parts.path.lstrip('/').partition('/')
    if (parts.hostname or '').lower() in HOSTS or host.lower() not in HOSTS:
        return cached
    return f'https://{host.lower()}/{rest}' + (f'?{parts.query}' if parts.query else '')


def snapshot(when=None, period='month'):
    """A fixed moment for Datafordeler GraphQL's registreringstid and virkningstid: the
    start of the UTC month (or day) of `when` (default now).

    The cache keys a GraphQL request on its body. With `at` = now, every body differs and
    nothing is ever found again; with the same snapshot, the same question has the same
    body, and the answer about a past moment does not change."""
    when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if period == 'month':
        return when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == 'day':
        return when.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError("period is 'month' or 'day'")
