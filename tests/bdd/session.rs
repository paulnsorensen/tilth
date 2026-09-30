use serde_json::{json, Value};
use std::{
    fmt, fs,
    io::{self, BufRead, BufReader, Write},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::mpsc::{self, Receiver, Sender},
    thread::{self, JoinHandle},
    time::Duration,
};
use tempfile::TempDir;

pub struct Session {
    child: Child,
    requests: Option<Sender<Value>>,
    responses: Receiver<io::Result<String>>,
    worker: Option<JoinHandle<()>>,
    root: TempDir,
    next_id: u64,
    last_request: Value,
    last_response: String,
}

impl Session {
    pub fn start() -> Self {
        let root = tempfile::tempdir().expect("create isolated scenario directory");
        let workspace = root.path().join("project");
        fs::create_dir_all(workspace.join("src")).expect("create fixture directory");
        for (path, content) in [
            ("Cargo.toml", include_str!("fixtures/project/Cargo.toml")),
            ("src/lib.rs", include_str!("fixtures/project/src/lib.rs")),
            (
                "src/helpers.rs",
                include_str!("fixtures/project/src/helpers.rs"),
            ),
        ] {
            fs::write(workspace.join(path), content).expect("copy fixture file");
        }
        let stderr = fs::File::create(root.path().join("stderr")).expect("create stderr log");
        let child = Command::new(env!("CARGO_BIN_EXE_tilth"))
            .args(["--mcp", "--edit"])
            .current_dir(&workspace)
            .env("XDG_CACHE_HOME", root.path().join("cache"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(stderr)
            .spawn()
            .expect("start tilth");
        let (requests, incoming) = mpsc::channel();
        let (outgoing, responses) = mpsc::channel();
        let mut session = Self {
            child,
            requests: Some(requests),
            responses,
            worker: None,
            root,
            next_id: 0,
            last_request: Value::Null,
            last_response: String::new(),
        };
        let mut stdin = session.child.stdin.take().expect("piped stdin");
        let mut stdout = BufReader::new(session.child.stdout.take().expect("piped stdout"));
        session.worker = Some(thread::spawn(move || {
            for request in incoming {
                let result = (|| {
                    writeln!(stdin, "{request}")?;
                    stdin.flush()?;
                    let mut line = String::new();
                    if stdout.read_line(&mut line)? == 0 {
                        return Err(io::Error::new(
                            io::ErrorKind::UnexpectedEof,
                            "tilth closed stdout",
                        ));
                    }
                    Ok(line)
                })();
                let failed = result.is_err();
                if outgoing.send(result).is_err() || failed {
                    break;
                }
            }
        }));
        let initialized = session.request(
            "initialize",
            &json!({
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "tilth-bdd", "version": "1"}
            }),
        );
        assert_eq!(initialized["serverInfo"]["name"], "tilth", "{session:?}");
        session
    }

    pub fn workspace(&self) -> PathBuf {
        self.root.path().join("project")
    }

    pub fn tool(&mut self, name: &str, mut arguments: Value) -> Value {
        arguments["cwd"] = json!(self.workspace());
        self.request("tools/call", &json!({"name": name, "arguments": arguments}))
    }

    fn request(&mut self, method: &str, params: &Value) -> Value {
        self.next_id += 1;
        self.last_request = json!({
            "jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params
        });
        self.requests
            .as_ref()
            .expect("live request channel")
            .send(self.last_request.clone())
            .unwrap_or_else(|error| panic!("send request: {error}\n{self:?}"));
        self.last_response = self
            .responses
            .recv_timeout(Duration::from_secs(15))
            .unwrap_or_else(|error| panic!("MCP request deadline or disconnect: {error}\n{self:?}"))
            .unwrap_or_else(|error| panic!("MCP transport: {error}\n{self:?}"));
        let response: Value = serde_json::from_str(&self.last_response)
            .unwrap_or_else(|error| panic!("invalid MCP JSON: {error}\n{self:?}"));
        assert_eq!(response["id"], self.next_id, "{self:?}");
        assert_eq!(response["error"], Value::Null, "{self:?}");
        response["result"].clone()
    }
}

impl fmt::Debug for Session {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Session")
            .field("workspace", &self.workspace())
            .field("pid", &self.child.id())
            .field("request", &self.last_request)
            .field("response", &self.last_response)
            .field(
                "stderr",
                &fs::read_to_string(self.root.path().join("stderr")),
            )
            .finish_non_exhaustive()
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        self.requests.take();
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}
