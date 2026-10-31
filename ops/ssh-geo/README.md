# SSH allowlist (one country + LAN + Tailscale)

For a Spark whose port 22 is forwarded by the NAT. This allows SSH only from addresses the RIPE NCC registry lists
for one country (`COUNTRY`, e.g. IL or DE), the LAN (`TRUSTED4`), Tailscale (tailscale0, 100.64.0.0/10), loopback,
and connections already open. Everything else on tcp/22 is dropped. It is an nftables table of its own
(`inet ssh_geo`) and touches no other rules. The RIPE file covers Europe, the Middle East and Central Asia only.

- Abroad, or on an address registered elsewhere: use Tailscale (`ssh you@your-spark`), which is always allowed.
- See the rule and the drop counter: `sudo nft list chain inet ssh_geo input`
- Refresh now: `sudo systemctl start sparkhub-ssh-geo`
- Turn it off: `sudo systemctl disable --now sparkhub-ssh-geo.timer sparkhub-ssh-geo.service && sudo nft delete table inet ssh_geo`

Install: `./install.sh` with `SSH_GEO=1`, `SSH_GEO_COUNTRY` and `LAN_CIDR` in `sparkhub.conf`. Keep a second session
open while it is applied. By hand (as root): write `/etc/default/sparkhub-ssh-geo` with `COUNTRY=..` and
`TRUSTED4="<your LAN> 100.64.0.0/10"` (there are no defaults), copy `sparkhub-ssh-geo` to /usr/local/sbin, `build.py`
to /usr/local/lib/sparkhub-ssh-geo/, the two units to /etc/systemd/system, then
`systemctl daemon-reload && systemctl enable --now sparkhub-ssh-geo.service sparkhub-ssh-geo.timer`.
