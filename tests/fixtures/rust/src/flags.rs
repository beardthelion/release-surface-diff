//! clap derive CLI surface (R9).

use clap::{Args, Parser, Subcommand};

#[derive(Parser)]
#[command(name = "node")]
pub struct Cli {
    #[arg(long)]
    pub verbose: bool,

    #[arg(long = "output-dir", short)]
    pub out: String,

    #[arg(short)]
    pub jobs: usize,

    pub positional_target: String,

    #[command(subcommand)]
    pub cmd: Commands,
}

#[derive(Subcommand)]
pub enum Commands {
    Scan,
    #[command(name = "diff-report")]
    DiffReport { repo: String },
}

#[derive(Args)]
pub struct NetArgs {
    #[arg(long)]
    pub listen_addr: String,
}
