# Included in every proxied location: nginx drops server-level proxy_set_header lines in any location that sets
# its own, so they live here instead.
proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Forwarded-For $remote_addr;    # replaces anything the client sent
proxy_set_header X-Forwarded-Proto https;
proxy_set_header X-Real-IP $remote_addr;
