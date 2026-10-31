# Port guard

For ports the NAT forwards but nothing should serve publicly yet (default: 8000). 8000 is also vLLM's (and many dev
servers') default port, so anything started on it would be public with no authentication. This keeps guarded ports
reachable from the LAN, Tailscale and the Spark itself only, and drops them from the internet, for programs on the
host and for Docker-published ports alike. It is an nftables table of its own (`inet port_guard`).

- See the rules and drop counters: `sudo nft list table inet port_guard`
- **Open a port to the internet for a specific service:** remove it from `PORT_GUARD_PORTS` in `sparkhub.conf` and
  run `./install.sh`. Do this only for a service that has its own authentication.
- Turn it off entirely: `sudo systemctl disable --now sparkhub-port-guard && sudo nft delete table inet port_guard`

Install: `./install.sh` with `PORT_GUARD=1`, `PORT_GUARD_PORTS` and `LAN_CIDR` in `sparkhub.conf`; it fills both sets
in `port-guard.nft`. By hand (as root): set your LAN in the `trusted4` set and the ports in `guarded`, copy
`port-guard.nft` to /usr/local/lib/sparkhub-port-guard/, the unit to /etc/systemd/system, then
`systemctl daemon-reload && systemctl enable --now sparkhub-port-guard`.
