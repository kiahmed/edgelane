# facades-portal: handle auth links that fall back to the portal

**For the facades-portal repo** (`public/index.html`). Not applied from EdgeLane.

The shared Supabase project's Site URL is now `https://facades.trade`, which is
GoTrue's fallback when an auth email (dashboard invite, admin-triggered reset,
email-change confirm) has no product redirect. Those links arrive with
`#access_token=…&refresh_token=…` (or `#error=…`) in the fragment.

The portal must strip that fragment **before any analytics runs** (gtag is
loaded at the top of `<head>`) and tell the user to open their product.

Paste as the **first** element inside `<head>`, before the gtag script:

```html
<script>
  // Supabase fallback landing: never keep auth tokens in the URL or let
  // analytics see them. Strip the fragment, then show a product chooser.
  (function () {
    var h = location.hash || "";
    if (!/(access_token|refresh_token|error_description|type=(signup|recovery|invite|email_change))/.test(h)) return;
    var isErr = /error/.test(h);
    history.replaceState(null, "", location.pathname + location.search);
    window.__FACADES_AUTH_FALLBACK__ = isErr ? "error" : "ok";
  })();
</script>
```

And near the end of `<body>`:

```html
<script>
  (function () {
    var s = window.__FACADES_AUTH_FALLBACK__;
    if (!s) return;
    var d = document.createElement("div");
    d.setAttribute("role", "status");
    d.style.cssText = "position:fixed;left:16px;right:16px;top:16px;z-index:99;padding:14px 16px;border-radius:10px;background:#0f172a;color:#e2e8f0;font:14px/1.5 system-ui;box-shadow:0 8px 24px rgba(0,0,0,.35)";
    d.textContent = s === "error"
      ? "That link is invalid or expired. Open your product and sign in again: "
      : "Your email is confirmed. Open your product and sign in: ";
    [["Matrix", "https://matrix.facades.trade/"], ["Simmer", "https://simmer.facades.trade/"],
     ["Reflex", "https://reflex.facades.trade/"]].forEach(function (p, i) {
      var a = document.createElement("a");
      a.href = p[1]; a.textContent = p[0];
      a.style.cssText = "color:#38bdf8;font-weight:600;margin-left:" + (i ? "10px" : "4px");
      d.appendChild(a);
    });
    document.body.appendChild(d);
  })();
</script>
```

The tokens are discarded, never stored or used, so the user signs in normally
on their product.
