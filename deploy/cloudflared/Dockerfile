# Cloudflare quick-tunnel + URL publisher.
#
# Runs a free `*.trycloudflare.com` quick tunnel to the backend and, on every
# (re)start, publishes the freshly-assigned hostname to Supabase app_config so
# the Vercel frontend can discover it at runtime (no redeploy on rotation).
#
# cloudflared ships a static Go binary; we lift it onto Alpine and add curl+jq
# for the publish step.
FROM cloudflare/cloudflared:latest AS cf

FROM alpine:3.20
RUN apk add --no-cache curl jq
COPY --from=cf /usr/local/bin/cloudflared /usr/local/bin/cloudflared
COPY publish-url.sh /usr/local/bin/publish-url.sh
RUN chmod +x /usr/local/bin/publish-url.sh
ENTRYPOINT ["/usr/local/bin/publish-url.sh"]
