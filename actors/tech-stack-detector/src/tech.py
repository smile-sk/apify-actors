"""Fingerprints and the detector.

A signature says: if one of these marks is on the page, this product was there. Every
signature carries the evidence that matched, because "Shopify" without proof is a guess
and a lead list built on guesses is worthless.

Marks come from four places a server cannot hide while it still wants the product to
work: response headers, cookies it sets, the html it serves and the script urls the
browser loads. A mark is one regex, and the first one that matches wins: a page does
not need three proofs to be WordPress.

The version is read only where the vendor itself prints it (a generator meta tag, a
versioned asset path). Everywhere else the field is empty on purpose: a wrong version
number is worse than no number.

A mark is anchored to the edge of its token: a product name found inside a longer word,
a hash or somebody else's file name is not that product. Both live mistakes that put a
wrong name on a paid row came from exactly this: `modx` inside the Cloudinary hash
`.../atbx42tpsb1vtmodxe0p.svg` on about.gitlab.com, and a Bootstrap pattern matching the
BBC's own `dotcom-bootstrap.js`. A hyphen and a pair of quotes are word boundaries too,
so the few names that need more than a boundary are written as an asset path or a quoted
asset instead of a bare word.
"""
from __future__ import annotations

import re

# A page can be megabytes of inline json. The marks we look for live in the head, in
# the script tags and in the first screens of markup, so the scan is capped: reading
# the whole body would cost memory for no extra answer.
MAX_SCAN_BYTES = 512 * 1024
MAX_EVIDENCE = 2

# Where a mark hides most often, next to the headers: the script urls the browser will
# load and the meta tags the page declares about itself.
SCRIPT_RE = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.I)
META_RE = re.compile(r"<meta\b[^>]*>", re.I)
META_NAME_RE = re.compile(r"(?:name|property|itemprop)\s*=\s*[\"']([^\"']+)[\"']", re.I)
META_CONTENT_RE = re.compile(r"content\s*=\s*[\"']([^\"']*)[\"']", re.I)

# key map: n=name, c=categories, h=header name -> pattern, k=cookie name,
#          p=page html pattern, s=script src pattern, m=meta name -> pattern
SIGNATURES: list[dict] = [
    # cms
    {"n": "WordPress", "c": ["cms"], "p": [r"wp-content/", r"wp-includes/"],
     "m": {"generator": r"wordpress(?:[ /](?P<version>[\d.]+))?"}},
    {"n": "Drupal", "c": ["cms"], "h": {"x-generator": r"drupal(?:[ /]?(?P<version>[\d.]+))?",
                                        "x-drupal-cache": r".+"},
     "m": {"generator": r"drupal(?:[ /]?(?P<version>[\d.]+))?"},
     "p": [r"drupalSettings", r"drupal\.js", r"/sites/default/files/", r"data-drupal-"]},
    {"n": "Joomla", "c": ["cms"], "m": {"generator": r"joomla!?(?:[ /](?P<version>[\d.]+))?"},
     "p": [r"/media/jui/", r"/components/com_"]},
    {"n": "Bitrix", "c": ["cms"], "p": [r"/bitrix/js/", r"/bitrix/templates/"]},
    {"n": "Tilda", "c": ["cms"], "p": [r"tildacdn\.com", r"tilda-blocks"]},
    {"n": "Wix", "c": ["cms"], "h": {"x-wix-request-id": r".+"},
     "p": [r"static\.parastorage\.com", r"wix\.com/xt"]},
    {"n": "Squarespace", "c": ["cms"], "p": [r"static1\.squarespace\.com", r"squarespace-cdn\.com"]},
    {"n": "Webflow", "c": ["cms"], "p": [r"\.w-webflow-badge", r"data-wf-page", r"webflow\.com/js"]},
    {"n": "Ghost", "c": ["cms", "blogs"], "m": {"generator": r"ghost(?:[ /](?P<version>[\d.]+))?"},
     "p": [r"ghost-admin-api"]},
    {"n": "HubSpot CMS", "c": ["cms", "marketing"], "p": [r"hubspot\.net/hubfs", r"hs-scripts\.com"]},
    {"n": "Craft CMS", "c": ["cms"], "k": ["CRAFT_CSRF_TOKEN"], "h": {"x-powered-by": r"craft cms"}},
    # A bare "modx" matched a Cloudinary hash on about.gitlab.com (atbx42tpsb1vtmodxe0p.svg)
    # and the buyer paid for a platform that site does not run. MODX is sold on its own
    # asset path, or on a quoted asset that carries the name as a whole word.
    {"n": "MODX", "c": ["cms"],
     "p": [r"/assets/components/", r"""["'][^"']*\bmodx\b[^"']*["']"""]},
    {"n": "Adobe Experience Manager", "c": ["cms"], "p": [r"/etc\.clientlibs/", r"/libs/granite/"]},
    {"n": "Sitecore", "c": ["cms"], "p": [r"/sitecore/", r"sc_mode="]},
    {"n": "Contentful", "c": ["cms"], "p": [r"ctfassets\.net", r"images\.ctfassets\.net"]},
    {"n": "Sanity", "c": ["cms"], "p": [r"cdn\.sanity\.io"]},
    {"n": "Prismic", "c": ["cms"], "p": [r"images\.prismic\.io"]},
    {"n": "Strapi", "c": ["cms"], "h": {"x-powered-by": r"strapi"}},
    {"n": "Elementor", "c": ["cms", "page-builder"], "p": [r"elementor-frontend", r"elementor-widget"]},
    {"n": "Divi", "c": ["cms", "page-builder"], "p": [r"et_divi", r"/divi/"]},
    {"n": "Yoast SEO", "c": ["seo"], "p": [r"yoast", r"yst-wpseo"]},
    {"n": "Rank Math", "c": ["seo"], "p": [r"rank-math"]},

    # ecommerce
    {"n": "Shopify", "c": ["ecommerce"], "h": {"x-shopid": r".+", "x-shopify-stage": r".+"},
     "p": [r"cdn\.shopify\.com", r"shopify\.theme", r"myshopify\.com"]},
    {"n": "WooCommerce", "c": ["ecommerce"], "p": [r"/wp-content/plugins/woocommerce/", r"woocommerce-page"]},
    {"n": "Magento", "c": ["ecommerce"], "p": [r"mage/cookies", r"/static/version\d+/", r"data-mage-init"],
     "h": {"x-magento-cache-debug": r".+"}},
    {"n": "BigCommerce", "c": ["ecommerce"], "p": [r"bigcommerce\.com", r"/stencil/"]},
    {"n": "PrestaShop", "c": ["ecommerce"], "p": [r"prestashop", r"/modules/ps_", r"var prestashop"]},
    {"n": "OpenCart", "c": ["ecommerce"], "p": [r"route=common/home", r"catalog/view/theme"]},
    {"n": "Shopware", "c": ["ecommerce"], "p": [r"/bundles/storefront/", r"shopware"]},
    {"n": "Salesforce Commerce Cloud", "c": ["ecommerce"], "p": [r"/on/demandware\.store/", r"demandware"]},
    {"n": "Ecwid", "c": ["ecommerce"], "p": [r"ecwid\.com/script\.js", r"ec-store"]},
    {"n": "Shopline", "c": ["ecommerce"], "p": [r"shoplineapp\.com"]},
    {"n": "CS-Cart", "c": ["ecommerce"], "p": [r"cscart", r"tygh/"]},
    {"n": "Volusion", "c": ["ecommerce"], "p": [r"volusion\.com", r"/v/vspfiles/"]},

    # frameworks and libraries
    {"n": "Next.js", "c": ["framework"], "p": [r"__NEXT_DATA__", r"/_next/static/"]},
    {"n": "Nuxt", "c": ["framework"], "p": [r"__NUXT__", r"/_nuxt/"]},
    {"n": "React", "c": ["framework", "js-library"], "p": [r"data-reactroot", r"/react-dom@",
                                                          r"react\.production\.min\.js", r"react-router"]},
    {"n": "Vue.js", "c": ["framework", "js-library"], "p": [r"data-v-[0-9a-f]{6,}", r"__VUE__",
                                                            r"vue\.runtime", r"vue\.min\.js"]},
    {"n": "Angular", "c": ["framework"], "p": [r"ng-version=", r"ng-app=", r"/@angular/"]},
    {"n": "Svelte", "c": ["framework"], "p": [r"svelte-[0-9a-z]{6}", r"__svelte"]},
    {"n": "SvelteKit", "c": ["framework"], "p": [r"__sveltekit", r"/_app/immutable/"]},
    {"n": "Gatsby", "c": ["framework"], "p": [r"___gatsby", r"/page-data\.json", r"gatsby-plugin"]},
    {"n": "Astro", "c": ["framework"], "p": [r"astro-island", r"/_astro/"]},
    {"n": "Remix", "c": ["framework"], "p": [r"__remixContext", r"/build/_assets/"]},
    {"n": "Ember.js", "c": ["framework"], "p": [r"ember-view", r"ember-cli"]},
    {"n": "Alpine.js", "c": ["js-library"], "p": [r"x-data=\"", r"alpinejs"]},
    {"n": "htmx", "c": ["js-library"], "p": [r"htmx\.min\.js", r"hx-get=\"", r"hx-post=\""]},
    {"n": "Stimulus", "c": ["js-library"], "p": [r"data-controller=\"", r"stimulus"]},
    {"n": "jQuery", "c": ["js-library"], "p": [r"jquery[.-](?P<version>\d+\.\d+\.\d+)?(?:\.min)?\.js",
                                               r"jquery\.min\.js"]},
    {"n": "Backbone.js", "c": ["js-library"], "p": [r"backbone(?:\.min)?\.js"]},
    # Without the left anchor this fired on the BBC's own dotcom-bootstrap.js: a word
    # boundary at a hyphen is still not an asset. Bootstrap is sold on a css or js asset,
    # or on a versioned CDN path.
    {"n": "Bootstrap", "c": ["ui"],
     "p": [r"""(?:^|[/"'@\s])bootstrap(?:\.bundle)?(?:\.min)?\.(?:css|js)""",
           r"bootstrap@(?P<version>\d+\.\d+\.\d+)"]},
    {"n": "Tailwind CSS", "c": ["ui"], "p": [r"cdn\.tailwindcss\.com", r"tailwind\.min\.css",
                                             r"tailwindcss/(dist|base)"]},
    {"n": "Material UI", "c": ["ui"], "p": [r"@mui/", r"mui\.min\.css", r"MuiButton"]},
    {"n": "Chakra UI", "c": ["ui"], "p": [r"chakra-ui", r"chakra\."]},
    {"n": "Bulma", "c": ["ui"], "p": [r"bulma(?:\.min)?\.css"]},
    {"n": "Foundation", "c": ["ui"], "p": [r"foundation(?:\.min)?\.(?:css|js)"]},

    # languages and server frameworks
    {"n": "PHP", "c": ["programming-language"], "h": {"x-powered-by": r"php/?(?P<version>[\d.]+)?"},
     "k": ["PHPSESSID"]},
    {"n": "ASP.NET", "c": ["framework", "programming-language"], "h": {"x-aspnet-version": r".+",
                                                                      "x-powered-by": r"asp\.net"},
     "p": [r"__viewstate", r"__eventvalidation"]},
    {"n": "Laravel", "c": ["framework"], "k": ["laravel_session"], "p": [r"laravel-mix", r"/vendor/laravel"]},
    {"n": "Django", "c": ["framework"], "k": ["csrftoken", "django_language"],
     "p": [r"csrfmiddlewaretoken", r"__admin_media_prefix__"]},
    {"n": "Ruby on Rails", "c": ["framework"], "k": ["_rails_session", "_session_id"],
     "p": [r"rails-ujs", r"action_dispatch"]},
    # The bare word fired on every blog post that mentions Symfony. The framework is sold
    # only on the paths it actually serves, and a page that only talks about it is free.
    {"n": "Symfony", "c": ["framework"],
     "p": [r"/vendor/symfony/", r"sf-toolbar", r"/symfony/profiler"]},
    {"n": "CodeIgniter", "c": ["framework"], "k": ["ci_session"]},
    {"n": "Yii", "c": ["framework"], "k": ["yii_csrf"]},
    {"n": "CakePHP", "c": ["framework"], "k": ["cakephp", "CAKEPHP"]},
    {"n": "Express", "c": ["framework"], "h": {"x-powered-by": r"express"}},
    {"n": "Flask", "c": ["framework"], "h": {"server": r"werkzeug"}},
    {"n": "Phoenix", "c": ["framework"], "p": [r"phoenix_html", r"phx-"]},
    {"n": "Spring", "c": ["framework"], "p": [r"org\.springframework"]},
    {"n": "Gunicorn", "c": ["server"], "h": {"server": r"gunicorn"}},
    {"n": "uvicorn", "c": ["server"], "h": {"server": r"uvicorn"}},

    # analytics
    {"n": "Google Analytics 4", "c": ["analytics"], "p": [r"googletagmanager\.com/gtag/js\?id=g-",
                                                          r"gtag\('config', *'g-"]},
    {"n": "Universal Analytics", "c": ["analytics"], "p": [r"google-analytics\.com/analytics\.js",
                                                           r"ga\('create'"]},
    {"n": "Google Tag Manager", "c": ["tag-manager"], "p": [r"googletagmanager\.com/gtm\.js",
                                                            r"gtm-[a-z0-9]{4,}"]},
    {"n": "Matomo", "c": ["analytics"], "p": [r"matomo\.js", r"piwik\.js", r"matomo\.php"]},
    {"n": "Plausible", "c": ["analytics"], "p": [r"plausible\.io/js"]},
    {"n": "Fathom", "c": ["analytics"], "p": [r"usefathom\.com"]},
    {"n": "Hotjar", "c": ["analytics"], "p": [r"static\.hotjar\.com", r"hjid:\s*\d+"]},
    {"n": "Mixpanel", "c": ["analytics"], "p": [r"cdn\.mxpnl\.com", r"mixpanel\.init"]},
    {"n": "Amplitude", "c": ["analytics"], "p": [r"cdn\.amplitude\.com"]},
    {"n": "Segment", "c": ["analytics"], "p": [r"cdn\.segment\.com", r"analytics\.js/v1"]},
    {"n": "Heap", "c": ["analytics"], "p": [r"heapanalytics\.com", r"cdn\.heap\.io"]},
    {"n": "Microsoft Clarity", "c": ["analytics"], "p": [r"clarity\.ms/tag"]},
    {"n": "Yandex Metrica", "c": ["analytics"], "p": [r"mc\.yandex\.ru/metrika"]},
    {"n": "Umami", "c": ["analytics"], "p": [r"umami\.is/script", r"umami\.js", r"data-website-id="]},
    {"n": "Statcounter", "c": ["analytics"], "p": [r"statcounter\.com"]},
    {"n": "Cloudflare Web Analytics", "c": ["analytics"], "p": [r"static\.cloudflareinsights\.com"]},

    # advertising
    {"n": "Google Ads", "c": ["advertising"], "p": [r"googleads\.g\.doubleclick\.net", r"/conversion\.js"]},
    {"n": "Meta Pixel", "c": ["advertising"], "p": [r"connect\.facebook\.net", r"fbq\('init'"]},
    {"n": "TikTok Pixel", "c": ["advertising"], "p": [r"analytics\.tiktok\.com"]},
    {"n": "LinkedIn Insight Tag", "c": ["advertising"], "p": [r"snap\.licdn\.com", r"px\.ads\.linkedin\.com"]},
    {"n": "Pinterest Tag", "c": ["advertising"], "p": [r"ct\.pinterest\.com"]},
    {"n": "Snap Pixel", "c": ["advertising"], "p": [r"sc-static\.net/scevent"]},
    {"n": "Criteo", "c": ["advertising"], "p": [r"static\.criteo\.net"]},
    {"n": "Taboola", "c": ["advertising"], "p": [r"cdn\.taboola\.com"]},
    {"n": "Outbrain", "c": ["advertising"], "p": [r"outbrain\.com/outbrain\.js"]},
    {"n": "AdRoll", "c": ["advertising"], "p": [r"adroll\.com"]},

    # marketing, crm and support
    {"n": "Mailchimp", "c": ["marketing", "email"], "p": [r"chimpstatic\.com", r"mailchimp\.com/mc"]},
    {"n": "Klaviyo", "c": ["marketing", "email"], "p": [r"klaviyo\.com", r"klaviyo\.js"]},
    {"n": "HubSpot", "c": ["marketing", "crm"], "p": [r"js\.hs-scripts\.com", r"hsforms\.net"]},
    {"n": "ActiveCampaign", "c": ["marketing", "email"], "p": [r"activehosted\.com", r"activecampaign"]},
    {"n": "Brevo", "c": ["marketing", "email"], "p": [r"sibautomation\.com", r"brevo"]},
    {"n": "ConvertKit", "c": ["marketing", "email"], "p": [r"convertkit\.com"]},
    {"n": "Marketo", "c": ["marketing"], "p": [r"munchkin\.marketo\.net"]},
    {"n": "Pardot", "c": ["marketing"], "p": [r"pi\.pardot\.com"]},
    {"n": "Customer.io", "c": ["marketing"], "p": [r"customer\.io"]},
    {"n": "Braze", "c": ["marketing"], "p": [r"appboy\.com"]},
    {"n": "Intercom", "c": ["support", "chat"], "p": [r"widget\.intercom\.io", r"intercomcdn\.com"]},
    {"n": "Drift", "c": ["support", "chat"], "p": [r"js\.driftt\.com", r"drift\.com"]},
    {"n": "Crisp", "c": ["support", "chat"], "p": [r"client\.crisp\.chat"]},
    {"n": "Tawk.to", "c": ["support", "chat"], "p": [r"embed\.tawk\.to"]},
    {"n": "LiveChat", "c": ["support", "chat"], "p": [r"livechatinc\.com"]},
    {"n": "Zendesk", "c": ["support"], "p": [r"zdassets\.com", r"zendesk\.com"]},
    {"n": "Freshdesk", "c": ["support"], "p": [r"freshdesk\.com", r"freshchat"]},
    {"n": "Optimizely", "c": ["ab-testing"], "p": [r"optimizely\.com/js", r"cdn\.optimizely\.com"]},
    {"n": "VWO", "c": ["ab-testing"], "p": [r"visualwebsiteoptimizer\.com", r"vwo\.js"]},

    # payments
    {"n": "Stripe", "c": ["payments"], "p": [r"js\.stripe\.com"]},
    {"n": "PayPal", "c": ["payments"], "p": [r"paypal\.com/sdk/js", r"paypalobjects\.com"]},
    {"n": "Braintree", "c": ["payments"], "p": [r"braintreegateway\.com", r"braintree-web"]},
    {"n": "Adyen", "c": ["payments"], "p": [r"checkoutshopper-live\.adyen\.com", r"adyen\.com/v"]},
    {"n": "Klarna", "c": ["payments"], "p": [r"klarna\.com", r"klarnaservices\.com"]},
    {"n": "Afterpay", "c": ["payments"], "p": [r"afterpay\.com", r"portal\.afterpay"]},
    {"n": "Square", "c": ["payments"], "p": [r"squareup\.com", r"js\.squarecdn\.com"]},
    {"n": "Razorpay", "c": ["payments"], "p": [r"checkout\.razorpay\.com"]},
    {"n": "PayU", "c": ["payments"], "p": [r"payu\.com", r"secure\.payu"]},
    {"n": "Mollie", "c": ["payments"], "p": [r"mollie\.com"]},
    {"n": "Checkout.com", "c": ["payments"], "p": [r"checkout\.com/sdk"]},

    # delivery, cdn and hosting
    {"n": "Cloudflare", "c": ["cdn", "security"], "h": {"cf-ray": r".+", "server": r"cloudflare"},
     "k": ["__cf_bm", "cf_clearance"]},
    {"n": "Amazon CloudFront", "c": ["cdn"], "h": {"x-amz-cf-id": r".+", "via": r"cloudfront"}},
    {"n": "Amazon S3", "c": ["hosting"], "h": {"server": r"amazons3"}},
    {"n": "Fastly", "c": ["cdn"], "h": {"x-served-by": r"cache-", "x-fastly-request-id": r".+"}},
    {"n": "Akamai", "c": ["cdn"], "h": {"x-akamai-transformed": r".+", "akamai-grn": r".+"}},
    {"n": "KeyCDN", "c": ["cdn"], "h": {"server": r"keycdn"}},
    {"n": "BunnyCDN", "c": ["cdn"], "h": {"server": r"bunnycdn"}},
    {"n": "jsDelivr", "c": ["cdn"], "p": [r"cdn\.jsdelivr\.net"]},
    {"n": "cdnjs", "c": ["cdn"], "p": [r"cdnjs\.cloudflare\.com"]},
    {"n": "unpkg", "c": ["cdn"], "p": [r"unpkg\.com"]},
    {"n": "Google Hosted Libraries", "c": ["cdn"], "p": [r"ajax\.googleapis\.com/ajax/libs"]},
    {"n": "Vercel", "c": ["hosting"], "h": {"server": r"vercel", "x-vercel-id": r".+"}},
    {"n": "Netlify", "c": ["hosting"], "h": {"server": r"netlify", "x-nf-request-id": r".+"}},
    {"n": "Firebase Hosting", "c": ["hosting"], "p": [r"firebaseio\.com", r"firebase\.js"]},
    {"n": "AWS Amplify", "c": ["hosting"], "p": [r"amplifyapp\.com"]},
    {"n": "Heroku", "c": ["hosting"], "h": {"via": r"vegur"}},
    {"n": "Render", "c": ["hosting"], "h": {"server": r"render"}},
    {"n": "Fly.io", "c": ["hosting"], "h": {"server": r"fly/[a-z0-9]+"}},
    {"n": "nginx", "c": ["server"], "h": {"server": r"nginx(?:/(?P<version>[\d.]+))?"}},
    {"n": "Apache", "c": ["server"], "h": {"server": r"apache(?:/(?P<version>[\d.]+))?"}},
    {"n": "Microsoft IIS", "c": ["server"], "h": {"server": r"microsoft-iis(?:/(?P<version>[\d.]+))?"}},
    {"n": "LiteSpeed", "c": ["server"], "h": {"server": r"litespeed"}},
    {"n": "Caddy", "c": ["server"], "h": {"server": r"caddy"}},
    {"n": "OpenResty", "c": ["server"], "h": {"server": r"openresty(?:/(?P<version>[\d.]+))?"}},

    # security, media, fonts
    {"n": "reCAPTCHA", "c": ["security"], "p": [r"recaptcha/api\.js", r"g-recaptcha"]},
    {"n": "hCaptcha", "c": ["security"], "p": [r"hcaptcha\.com/1/api\.js", r"h-captcha"]},
    {"n": "Sucuri", "c": ["security", "cdn"], "h": {"x-sucuri-id": r".+"}},
    {"n": "YouTube", "c": ["video"], "p": [r"youtube\.com/embed/", r"youtube-nocookie\.com"]},
    {"n": "Vimeo", "c": ["video"], "p": [r"player\.vimeo\.com"]},
    {"n": "Wistia", "c": ["video"], "p": [r"fast\.wistia\.com", r"wistia\.com/embed"]},
    {"n": "Google Fonts", "c": ["fonts"], "p": [r"fonts\.googleapis\.com", r"fonts\.gstatic\.com"]},
    {"n": "Font Awesome", "c": ["fonts"], "p": [r"fontawesome", r"font-awesome"]},
    {"n": "Adobe Fonts", "c": ["fonts"], "p": [r"use\.typekit\.net"]},
]

_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"


def _anchored(pattern: str) -> str:
    """A name must stand on its own: not inside a hash, a file name or a longer word.

    The anchor goes only on the edge that is a word character, so a mark written as a
    path keeps matching the rest of the path: `/wp-content/` still fires inside
    `/wp-content/themes/twentytwenty/style.css`.
    """
    if not pattern:
        return pattern
    body = f"(?:{pattern})"
    if pattern[:1].lower() in _ALNUM:
        body = "(?<![a-z0-9])" + body
    if pattern[-1:].lower() in _ALNUM:
        body = body + "(?![a-z0-9])"
    return body


_COMPILED: dict[str, list[tuple[str, str, re.Pattern]]] | None = None


def _compiled() -> dict[str, list[tuple[str, str, re.Pattern]]]:
    """Every pattern compiled once, grouped by where it is looked for."""
    global _COMPILED
    if _COMPILED is not None:
        return _COMPILED
    table: dict[str, list[tuple[str, str, re.Pattern]]] = {
        "h": [], "k": [], "p": [], "s": [], "m": [],
    }
    for sig in SIGNATURES:
        name = sig["n"]
        for header, pattern in (sig.get("h") or {}).items():
            table["h"].append((name, header.lower(), re.compile(pattern, re.I)))
        for cookie in sig.get("k") or []:
            table["k"].append((name, cookie.lower(), re.compile(re.escape(cookie), re.I)))
        for pattern in sig.get("p") or []:
            table["p"].append((name, pattern, re.compile(_anchored(pattern), re.I)))
        for pattern in sig.get("s") or []:
            table["s"].append((name, pattern, re.compile(pattern, re.I)))
        for meta, pattern in (sig.get("m") or {}).items():
            table["m"].append((name, meta.lower(), re.compile(pattern, re.I)))
    for key in table:
        table[key] = sorted(table[key], key=lambda item: item[0])
    _COMPILED = table
    return table


def categories_of(name: str) -> list[str]:
    for sig in SIGNATURES:
        if sig["n"] == name:
            return list(sig.get("c") or [])
    return []


def parse_page(html: str) -> tuple[list[str], dict]:
    """Script urls and meta tags: the two places a mark hides most often."""
    text = html or ""
    scripts = [match.group(1).strip() for match in SCRIPT_RE.finditer(text)][:200]
    metas: dict[str, list[str]] = {}
    for tag in META_RE.finditer(text):
        name = META_NAME_RE.search(tag.group(0))
        content = META_CONTENT_RE.search(tag.group(0))
        if not name or not content:
            continue
        key = name.group(1).strip().lower()
        if key in ("name", "property", "charset", "http-equiv"):
            continue
        metas.setdefault(key, []).append(content.group(1).strip())
    return scripts, metas


def page_signals(html: str, headers: dict, cookies: list[str], scripts=None,
                 metas=None) -> dict:
    """Everything the detector looks at, cut to the part that can carry a mark."""
    if scripts is None or metas is None:
        parsed_scripts, parsed_metas = parse_page(html)
        scripts = parsed_scripts if scripts is None else scripts
        metas = parsed_metas if metas is None else metas
    head = (html or "")[:MAX_SCAN_BYTES]
    lowered = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
    return {
        "h": lowered,
        "k": [str(item).lower() for item in (cookies or [])],
        "p": head,
        "s": [str(item) for item in (scripts or [])],
        "m": {str(key).lower(): [str(value) for value in values]
              for key, values in (metas or {}).items()},
    }


def detect(signals: dict) -> list[dict]:
    """Technologies found on the page, each with the evidence that matched."""
    table = _compiled()
    found: dict[str, dict] = {}
    for kind in ("h", "k", "s", "m", "p"):
        for name, needle, regex in table[kind]:
            if name in found and found[name].get("confidence") == "high":
                continue
            evidence, version = _probe(kind, needle, regex, signals)
            if evidence is None:
                continue
            item = found.setdefault(name, {
                "name": name,
                "categories": categories_of(name),
                "confidence": "high" if kind in ("h", "k", "m") else "medium",
                "evidence": [],
                "version": None,
            })
            if kind in ("h", "k", "m"):
                item["confidence"] = "high"
            if evidence not in item["evidence"] and len(item["evidence"]) < MAX_EVIDENCE:
                item["evidence"].append(evidence)
            if version and not item["version"]:
                item["version"] = version
    return sorted(found.values(), key=lambda item: (item["categories"], item["name"]))


def _probe(kind: str, needle: str, regex: re.Pattern, signals: dict) -> tuple[str | None, str | None]:
    """The evidence sentence for one mark, or None when the mark is not on the page."""
    if kind == "h":
        value = (signals.get("h") or {}).get(needle)
        if not value:
            return None, None
        match = regex.search(value)
        if not match:
            return None, None
        return f"header {needle}: {value[:80]}", _version(match)
    if kind == "k":
        for cookie in signals.get("k") or []:
            if regex.search(cookie):
                return f"cookie {needle}", None
        return None, None
    if kind == "s":
        for src in signals.get("s") or []:
            match = regex.search(src)
            if match:
                return f"script {src[:120]}", _version(match)
        return None, None
    if kind == "m":
        for value in (signals.get("m") or {}).get(needle, []):
            match = regex.search(value)
            if match:
                return f"meta {needle}: {value[:80]}", _version(match)
        return None, None
    body = signals.get("p") or ""
    match = regex.search(body)
    if not match:
        return None, None
    start = max(0, match.start() - 20)
    snippet = body[start:match.end() + 20].replace("\n", " ").replace("\r", " ").strip()
    return f"html: {snippet[:120]}", _version(match)


def _version(match: re.Match) -> str | None:
    """The version a vendor printed, when the pattern asked for it."""
    try:
        value = match.group("version")
    except IndexError:
        return None
    return value or None


def categories_present(items: list[dict]) -> list[str]:
    seen: list[str] = []
    for item in items:
        for category in item.get("categories") or []:
            if category not in seen:
                seen.append(category)
    return sorted(seen)


def names_lower(items: list[dict]) -> set[str]:
    return {str(item.get("name") or "").lower() for item in items}


def normalise_wanted(wanted) -> list[str]:
    """A filter list as lowercase names, with nothing empty inside."""
    result: list[str] = []
    for raw in wanted or []:
        text = str(raw or "").strip().lower()
        if text and text not in result:
            result.append(text)
    return result


def matches(items: list[dict], wanted: list[str], forbidden: list[str]) -> tuple[bool, str | None]:
    """Whether a row passes the buyer's filters, and the plain reason when it does not.

    A name filter matches a part of the technology name on purpose: a buyer who types
    'shopify' wants Shopify and 'Shopify Plus', and one who types 'next' gets Next.js.
    """
    names = names_lower(items)
    if wanted:
        hit = [want for want in wanted if any(want in name for name in names)]
        if not hit:
            return False, f"none of the wanted technologies is on the page ({', '.join(wanted)})"
    if forbidden:
        hit = [ban for ban in forbidden if any(ban in name for name in names)]
        if hit:
            return False, f"the page uses a technology the buyer excluded ({', '.join(hit)})"
    return True, None
