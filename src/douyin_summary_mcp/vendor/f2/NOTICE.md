# F2 A-Bogus signature attribution

Source: Johnserf-Seed/f2, Apache License 2.0.
Pinned revision: f6be8c0ffba9a127075bbeafe4838716650b6325.
Original file: f2/utils/crypto/bytedance/abogus.py.
https://github.com/Johnserf-Seed/f2/tree/f6be8c0ffba9a127075bbeafe4838716650b6325

The algorithm file is copied unchanged. Zephyr only imports ABogus and
BrowserFingerprintGenerator; F2 crawler, browser automation and other platforms
are not included. gmssl is the only additional algorithm dependency.
Upstream author notices remain in abogus.py. Full license is in LICENSE.

douyin.py also adapts the native detail parameters from f2/apps/douyin/model.py
and the narrow header rules from GatewayHeaderManager in f2/apps/douyin/utils.py
at the same revision. The latter is rewritten inline, restricted to Douyin web
requests; dedicated Cookie/UIFID values are never sent to media or other hosts.
