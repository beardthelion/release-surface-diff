//! serde config structs (R9); renames are honored, nested structs give
//! dotted config_key paths.

use serde::Deserialize;

#[derive(Deserialize)]
pub struct NodeConfig {
    pub datadir: String,
    #[serde(rename = "http-port")]
    pub http_port: u16,
    #[serde(rename = "p2p")]
    pub network: P2pConfig,
    pub metrics: MetricsToml,
    #[serde(flatten)]
    pub common: CommonConfig,
    pub labels: std::collections::HashMap<String, String>,
}

#[derive(Deserialize)]
pub struct P2pConfig {
    pub port: u16,
    #[serde(rename = "max-peers")]
    pub max_peers: usize,
    #[serde(rename(deserialize = "boot-nodes"))]
    pub boot_nodes: Vec<String>,
}

#[derive(Deserialize)]
pub struct CommonConfig {
    #[serde(rename = "log-level")]
    pub log_level: String,
}

// Not a serde struct: no config_key records from here.
pub struct MetricsToml {
    pub enabled: bool,
    pub listen: String,
}
