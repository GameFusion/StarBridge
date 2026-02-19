import socket
import json
import platform
from datetime import datetime, timezone

try:
    import netifaces
    HAS_NETIFACES = True
except ImportError:
    HAS_NETIFACES = False

def get_local_ip_addresses() -> dict:
    """
    Returns clean, flat local IP structure:
    - ipv4[] and ipv6[] at top level (all unique)
    - primary_ipv4 / primary_ipv6 = first real (non-localhost) IP
    - Includes localhost for completeness
    - No interfaces, no fallback noise
    """
    result = {
        "ipv4": [],
        "ipv6": [],
        "primary_ipv4": None,
        "primary_ipv6": None,
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec='milliseconds') + "Z"
    }

    seen_ipv4 = set()
    seen_ipv6 = set()

    try:
        if HAS_NETIFACES:
            for iface in netifaces.interfaces():
                addrs = netifaces.ifaddresses(iface)

                # IPv4
                if netifaces.AF_INET in addrs:
                    for addr in addrs[netifaces.AF_INET]:
                        ip = addr['addr']
                        if ip not in seen_ipv4:
                            seen_ipv4.add(ip)
                            result["ipv4"].append(ip)
                            if result["primary_ipv4"] is None and not ip.startswith("127."):
                                result["primary_ipv4"] = ip

                # IPv6
                if netifaces.AF_INET6 in addrs:
                    for addr in addrs[netifaces.AF_INET6]:
                        ip = addr['addr'].split('%')[0]  # strip scope
                        if ip not in seen_ipv6:
                            seen_ipv6.add(ip)
                            result["ipv6"].append(ip)
                            if result["primary_ipv6"] is None and not ip.startswith("fe80::") and ip != "::1":
                                result["primary_ipv6"] = ip

        else:
            # Fallback: socket method
            hostname = socket.gethostname()

            # IPv4
            try:
                for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                    ip = info[4][0]
                    if ip not in seen_ipv4:
                        seen_ipv4.add(ip)
                        result["ipv4"].append(ip)
                        if result["primary_ipv4"] is None and not ip.startswith("127."):
                            result["primary_ipv4"] = ip
            except:
                pass

            # IPv6
            try:
                for info in socket.getaddrinfo(hostname, None, socket.AF_INET6):
                    ip = info[4][0].split('%')[0]
                    if ip not in seen_ipv6:
                        seen_ipv6.add(ip)
                        result["ipv6"].append(ip)
                        if result["primary_ipv6"] is None and not ip.startswith("fe80::") and ip != "::1":
                            result["primary_ipv6"] = ip
            except:
                pass

            # On Linux: UDP trick for primary IPv4 if still None
            if result["primary_ipv4"] is None and platform.system() == "Linux":
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    ip = s.getsockname()[0]
                    s.close()
                    if ip not in seen_ipv4:
                        seen_ipv4.add(ip)
                        result["ipv4"].append(ip)
                    result["primary_ipv4"] = ip
                except:
                    pass

        # Include localhost if missing
        if "127.0.0.1" not in seen_ipv4:
            result["ipv4"].append("127.0.0.1")
        if "::1" not in seen_ipv6:
            result["ipv6"].append("::1")

        # Sort: real IPs first
        result["ipv4"] = sorted(result["ipv4"], key=lambda x: x.startswith("127."))
        result["ipv6"] = sorted(result["ipv6"], key=lambda x: x in ["::1", "fe80::"])

    except Exception as e:
        result["error"] = str(e)

    return result

if __name__ == "__main__":
    ret = get_local_ip_addresses()
    print("local ip addresses", json.dumps(ret, indent=4))
