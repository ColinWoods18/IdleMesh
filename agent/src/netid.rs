//! The node's network identity (S5.1): the subnet of the interface it uses to reach the coordinator.
//!
//! Nodes on one subnet form a cluster (D-24), so the coordinator needs each node's subnet. The agent asks the
//! OS which local address it would use to reach the coordinator (a UDP "connect", which sends nothing), then
//! finds that address among the interfaces to learn its prefix. Only the network address and prefix are
//! reported, never the host address.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, ToSocketAddrs, UdpSocket};

/// The subnet, such as "10.10.132.0/24", used to reach `coordinator` (a URL), or None if it cannot be told.
pub fn subnet_towards(coordinator: &str) -> Option<String> {
    let local = local_addr_towards(coordinator)?;
    if local.is_loopback() {  // not listed as an interface on every OS (Windows)
        return Some(network(local, if local.is_ipv4() { 8 } else { 128 }));
    }
    let nets = sysinfo::Networks::new_with_refreshed_list();
    let prefix = nets.values()
        .flat_map(|data| data.ip_networks().iter())
        .find(|n| n.addr == local)
        .map(|n| n.prefix)?;
    Some(network(local, prefix))
}

fn local_addr_towards(coordinator: &str) -> Option<IpAddr> {
    let rest = coordinator.split("://").nth(1).unwrap_or(coordinator);
    let authority = rest.split('/').next()?;
    let target: SocketAddr = if authority.contains(':') && !authority.ends_with(']') {
        authority.to_socket_addrs().ok()?.next()?
    } else {
        (authority.trim_start_matches('[').trim_end_matches(']'), 443).to_socket_addrs().ok()?.next()?
    };
    let bind: SocketAddr = if target.is_ipv4() { "0.0.0.0:0".parse().ok()? } else { "[::]:0".parse().ok()? };
    let sock = UdpSocket::bind(bind).ok()?;
    sock.connect(target).ok()?;
    Some(sock.local_addr().ok()?.ip())
}

/// The network address of `addr` with `prefix` bits, as CIDR text.
pub fn network(addr: IpAddr, prefix: u8) -> String {
    match addr {
        IpAddr::V4(a) => {
            let p = prefix.min(32);
            let mask = if p == 0 { 0 } else { u32::MAX << (32 - p) };
            format!("{}/{p}", Ipv4Addr::from(u32::from(a) & mask))
        }
        IpAddr::V6(a) => {
            let p = prefix.min(128);
            let mask = if p == 0 { 0 } else { u128::MAX << (128 - p) };
            format!("{}/{p}", Ipv6Addr::from(u128::from(a) & mask))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn network_addresses_are_masked() {
        assert_eq!(network("10.10.132.248".parse().unwrap(), 24), "10.10.132.0/24");
        assert_eq!(network("10.100.10.19".parse().unwrap(), 16), "10.100.0.0/16");
        assert_eq!(network("192.168.1.7".parse().unwrap(), 32), "192.168.1.7/32");
        assert_eq!(network("192.168.1.7".parse().unwrap(), 0), "0.0.0.0/0");
        assert_eq!(network("fe80::1234:5678".parse().unwrap(), 64), "fe80::/64");
    }

    #[test]
    fn the_loopback_coordinator_is_reached_from_loopback() {
        assert_eq!(local_addr_towards("https://127.0.0.1:8765"), Some("127.0.0.1".parse().unwrap()));
        assert_eq!(local_addr_towards("http://127.0.0.1"), Some("127.0.0.1".parse().unwrap()));
        assert_eq!(subnet_towards("https://127.0.0.1:8765").as_deref(), Some("127.0.0.0/8"));
    }

    #[test]
    #[ignore = "depends on this machine's network; run with --ignored to see the LAN subnet"]
    fn shows_the_lan_subnet() {
        println!("subnet towards 10.10.132.248: {:?}", subnet_towards("https://10.10.132.248:8765"));
        println!("subnet towards 10.100.10.19: {:?}", subnet_towards("https://10.100.10.19:8765"));
    }
}
