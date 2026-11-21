# Sagemcom Router (Home Assistant custom integration)

Read-only statistics from Sagemcom routers that run the newer REST web UI
(tested: DELTA Fiber **F5598T**, firmware SGQA530011400P). Domain: `sagemcom_rest`.

## Install (HACS)
1. HACS → ⋮ → Custom repositories → add `https://github.com/Senexis/ha-sagemcom-router`
   with type **Integration**.
2. Download "Sagemcom Router", restart Home Assistant.
3. Settings → Devices & services → Add integration → "Sagemcom Router"
   (defaults: host `192.168.1.254`, user `beheer`).

The router and its network clients are shown in separate groups ("Router" and
"Network clients") on the integration page.

## Entities
- **Internet**: connectivity binary sensor, status, connected-since, public IPv4/IPv6
  (IPv4 has a `carrier_grade_nat` attribute), WAN link speed.
- **Traffic**: download/upload throughput (Mbit/s, computed from WAN byte counters),
  total downloaded/uploaded (total_increasing, survives router reboots).
- **Clients**: connected / Wi-Fi / wired device counts. `sensor.*_connected_devices`
  has a `devices` attribute (name, IP, band, signal), meant for dashboards and AI summaries.
- **Wi-Fi per band (2.4/5/6 GHz)**: up/down, clients, channel (+ bandwidth, standard),
  per-band traffic (disabled by default). Guest Wi-Fi on/off.
- **System**: CPU, memory, temperature (+ problem sensor), NAT table usage, last boot,
  firmware, mesh nodes, network configuration (DNS/DHCP/UPnP/firewall/NTP).
- **Network clients**: one device per client with a device tracker (disabled by
  default; enable the ones you want for presence). Tracker attributes: connection,
  network (main/guest), band, SSID, signal, link rate both ways, speed of the wired
  port it sits behind, DHCP/static, DHCP reservation, router's device-type guess.
  Optional per-client sensors (disabled by default): connection, signal strength,
  link speed.
- Diagnostics download (redacted).

Technical sensors (CPU, memory, temperature, NAT table, firmware, network
configuration, Wi-Fi channels, port and WAN link speeds, IPv6 address, time sync,
Wi-Fi/wired device counts) exist but are disabled by default, to keep the recorder
lean. Enable them per entity if you want them.

## Controls (optional)
Off by default. Turn on **Enable controls** in the integration's options to add
entities that change router settings:
- **Guest Wi-Fi** switch (all guest bands at once).
- **Wi-Fi radio** switch per band (2.4/5/6 GHz). Turning a band off drops every
  device on it, guest included.
- **LEDs** light: off or 25/50/75/100 % brightness.
- **Start WPS pairing** button (push-button pairing on all bands).
- **Block internet** switch per network client, using the router's parental
  control ("always block" rule for that MAC). Devices with a time schedule made
  in the router UI are left alone.
- **Forget device** button per network client: removes an offline client from the
  router's device list and from Home Assistant.
- **Reboot** button, disabled by default.

Factory reset, firmware upgrade, backup/restore and WAN settings are deliberately
not exposed.

## API notes
- Login: `POST /api/v2/login-params` -> salt/nonce, SHA-512-crypt challenge, then
  `POST /api/v1/login`; send cookie `__Host-csrf_token` as header `X-CSRF-Token`.
  aiohttp needs `CookieJar(unsafe=True)` (IP host). An expired session gives HTTP 400.
- Mesh/station data comes from `/api/v4/easymesh/meshdevices` (~1.5 s, every poll).
  `/api/v2/easymesh/meshdevices` has the same data but takes ~20 s; it is only used
  as a fallback. `/api/v2/wireless/assoclist/*` (~6 s each) isn't used.
- `/api/v2/wireless/<band>` and `/api/v2/home` return Wi-Fi passphrases in plain text.
  `api.scrub()` strips credential-like keys from everything before it's stored.
- Writes use flat form-encoded bodies (like the web UI), booleans as `true`/`false`,
  guest SSID `enable` as `1`/`0`. Reads stay GET-only. The client logs out on
  unload, because the router has a limited number of web sessions.

## Releasing
Bump `version` in `custom_components/sagemcom_rest/manifest.json`, commit, then
push a matching tag (`git tag v0.3.1 && git push --tags`). The release workflow
checks the tag against the manifest and publishes a GitHub release; HACS then
offers the update in Home Assistant.
