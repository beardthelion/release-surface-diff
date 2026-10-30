//! RPC surface: tonic add_service, jsonrpsee proc-macro + manual registration.

use jsonrpsee::core::RpcResult;
use jsonrpsee::proc_macros::rpc;
use jsonrpsee::RpcModule;
use tonic::transport::Server;

pub struct Proof;
pub struct Greeter;

#[rpc(server, namespace = "eth")]
pub trait EthApi {
    #[method(name = "eth_getProof")]
    async fn get_proof(&self) -> RpcResult<Proof>;

    #[method(name = "eth_call", blocking)]
    fn call(&self) -> RpcResult<()>;

    #[subscription(name = "eth_subscribe", item = String)]
    async fn subscribe(&self) -> RpcResult<()>;

    async fn block_number(&self) -> RpcResult<u64>;
}

pub mod greeter {
    pub struct GreeterService;
    pub struct GreeterServer;
    impl GreeterServer {
        pub fn new(_svc: GreeterService) -> Self {
            GreeterServer
        }
    }
}

pub struct EthApiServer;
impl EthApiServer {
    pub fn new<T>(_svc: T) -> Self {
        EthApiServer
    }
}

pub fn serve(builder: tonic::transport::server::Router, api: EthApiServer) {
    builder
        .add_service(greeter::GreeterServer::new(greeter::GreeterService))
        .add_service(EthApiServer::new(api));
}

fn manual(module: &mut RpcModule<()>) {
    module
        .register_method("debug_traceCall", |_p, _c| Ok(()))
        .unwrap();
    module
        .register_async_method("eth_syncing", |_, _| async move { Ok(true) })
        .unwrap();
}
