//! libp2p protocol registrations. The helper emits only structured
//! registrations whose argument is not a bare string literal; literal
//! protocol strings belong to the orchestrator's protocol_ids pass.

use libp2p::StreamProtocol;

const BEACON_STATUS: &str = "/eth2/beacon_chain/req/status/1/ssz_snappy";

pub fn protocols() {
    // Bare literal: orchestrator-owned, helper must not emit.
    let _a = StreamProtocol::new("/eth2/beacon_chain/req/status/1/ssz_snappy");
    // Const reference: helper emits a structured record.
    let _b = StreamProtocol::new(BEACON_STATUS);
    // concat! of literals: resolved here, still a protocol id.
    let _c = StreamProtocol::new(concat!("/eth2/beacon_chain/req/blocks/", "1/ssz_snappy"));
    let _d = other::IdentifyProtocol::new(Self::PROTO);
}
