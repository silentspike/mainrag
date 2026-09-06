//! Fresh-process physical pack measurements. No SQL, ingestion or device-I/O claims.
use super::*;
use anyhow::{ensure, Context};
use std::process::Stdio;
use std::time::{Duration, Instant};

struct Root(PathBuf);
impl Drop for Root {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

struct Fixture {
    remaining: u64,
    state: u64,
    random: bool,
}
impl Read for Fixture {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        let count = out.len().min(self.remaining as usize);
        for chunk in out[..count].chunks_mut(8) {
            self.state ^= self.state << 13;
            self.state ^= self.state >> 7;
            self.state ^= self.state << 17;
            let bytes = if self.random {
                self.state.to_le_bytes()
            } else {
                [b'x'; 8]
            };
            chunk.copy_from_slice(&bytes[..chunk.len()]);
        }
        self.remaining -= count as u64;
        Ok(count)
    }
}

fn peak_rss_bytes() -> anyhow::Result<u64> {
    let status = fs::read_to_string("/proc/self/status")?;
    let line = status
        .lines()
        .find_map(|line| line.strip_prefix("VmHWM:"))
        .context("VmHWM unavailable")?;
    let fields: Vec<_> = line.split_whitespace().collect();
    ensure!(
        fields.len() == 2 && fields[1] == "kB",
        "unexpected VmHWM units"
    );
    fields[0]
        .parse::<u64>()?
        .checked_mul(1024)
        .context("RSS overflow")
}

#[test]
#[ignore = "owned fresh-process resource probe; invoked by pack_resource_matrix"]
fn pack_resource_child() -> anyhow::Result<()> {
    let owner: Uuid = std::env::var("MAINRAG_PACK_RESOURCE_OWNER")?.parse()?;
    let case: Uuid = std::env::var("MAINRAG_PACK_RESOURCE_CASE")?.parse()?;
    let root = std::env::temp_dir()
        .join(format!("mainrag-pack-resource-{owner}"))
        .join(case.to_string());
    ensure!(root.is_dir(), "fixture parent must create the root");
    let buffer: usize = std::env::var("MAINRAG_PACK_RESOURCE_BUFFER")?.parse()?;
    let large: u64 = std::env::var("MAINRAG_PACK_RESOURCE_LARGE")?.parse()?;
    let pattern = std::env::var("MAINRAG_PACK_RESOURCE_PATTERN")?;
    let codec_name = std::env::var("MAINRAG_PACK_RESOURCE_CODEC")?;
    ensure!([4096, 65536].contains(&buffer) && [1048576, 16777216].contains(&large));
    ensure!(["repeat", "random"].contains(&pattern.as_str()));
    let codec = match codec_name.as_str() {
        "identity" => BodyCodec::Identity,
        "zstd" => BodyCodec::Zstd,
        _ => anyhow::bail!("invalid fixture codec"),
    };
    let sizes = [4096, 262144, large];
    let logical: u64 = sizes.iter().sum();
    let baseline_rss = peak_rss_bytes()?;
    let started = Instant::now();
    let mut source = PackBuilder::new(&root, Uuid::new_v4(), Uuid::new_v4(), buffer)?;
    for size in sizes {
        source.add_reader(
            Fixture {
                remaining: size,
                state: 42,
                random: pattern == "random",
            },
            BodyCodec::Identity,
            None,
        )?;
    }
    let source = source.seal()?.publish()?;
    let build_ms = started.elapsed().as_secs_f64() * 1000.0;
    let rewrite_start = Instant::now();
    let mut replacement = PackBuilder::new(&root, Uuid::new_v4(), Uuid::new_v4(), buffer)?;
    for entry in &source.manifest.entries {
        source
            .reader()
            .verify_to_staging(entry, None, &root, buffer)?
            .repack_into(&mut replacement, codec)?;
    }
    let replacement = replacement.seal()?.publish()?;
    let rewrite_ms = rewrite_start.elapsed().as_secs_f64() * 1000.0;
    let verify_start = Instant::now();
    for (old, new) in source
        .manifest
        .entries
        .iter()
        .zip(&replacement.manifest.entries)
    {
        ensure!(old.body == new.body, "rewrite changed content identity");
        replacement.reader().verify_integrity(new, None, buffer)?;
    }
    ensure!(replacement.manifest.entries.len() == sizes.len());
    let verify_ms = verify_start.elapsed().as_secs_f64() * 1000.0;
    let peak = peak_rss_bytes()?;
    ensure!(
        peak >= baseline_rss && peak <= 128 * 1024 * 1024,
        "fixture process exceeds 128 MiB RSS gate"
    );
    println!(
        "PACK_RESOURCE {}",
        serde_json::json!({
            "schema":"pack-resource-v1", "scope":"physical_pack_only", "profile":if cfg!(debug_assertions) {"debug"} else {"release"},
            "pattern":pattern,"codec":codec_name,"buffer_bytes":buffer,"large_body_bytes":large,
            "logical_bytes":logical,"stored_bytes":replacement.manifest.stored_bytes,"source_stored_bytes":source.manifest.stored_bytes,
            "build_ms":build_ms,"rewrite_ms":rewrite_ms,"verify_ms":verify_ms,
            "rewrite_mib_s": logical as f64 / (rewrite_ms/1000.0) / 1048576.0,
            "process_peak_rss_bytes":peak,"process_baseline_hwm_bytes":baseline_rss,
            "integrity_passed":1,"entry_count":sizes.len(),"sql_ms":null,"device_io_bytes":null
        })
    );
    Ok(())
}

#[tokio::test]
#[ignore = "explicit serial fresh-process resource matrix; no production policy qualification"]
async fn pack_resource_matrix() -> anyhow::Result<()> {
    let owner = Uuid::new_v4();
    let root = Root(std::env::temp_dir().join(format!("mainrag-pack-resource-{owner}")));
    fs::create_dir(&root.0)?;
    let mut count = 0;
    for repetition in 1..=3 {
        for large in [1048576_u64, 16777216] {
            for pattern in ["repeat", "random"] {
                // Alternate order to avoid always assigning cache/temperature drift to one setting.
                for codec in if repetition % 2 == 1 {
                    ["identity", "zstd"]
                } else {
                    ["zstd", "identity"]
                } {
                    for buffer in if repetition % 2 == 1 {
                        [4096, 65536]
                    } else {
                        [65536, 4096]
                    } {
                        let case = Uuid::new_v4();
                        let case_root = root.0.join(case.to_string());
                        fs::create_dir(&case_root)?;
                        let child = tokio::process::Command::new(std::env::current_exe()?)
                            .args([
                                "services::content_store::resource_tests::pack_resource_child",
                                "--ignored",
                                "--exact",
                                "--nocapture",
                            ])
                            .env("MAINRAG_PACK_RESOURCE_OWNER", owner.to_string())
                            .env("MAINRAG_PACK_RESOURCE_CASE", case.to_string())
                            .env("MAINRAG_PACK_RESOURCE_BUFFER", buffer.to_string())
                            .env("MAINRAG_PACK_RESOURCE_LARGE", large.to_string())
                            .env("MAINRAG_PACK_RESOURCE_PATTERN", pattern)
                            .env("MAINRAG_PACK_RESOURCE_CODEC", codec)
                            .stdin(Stdio::null())
                            .stderr(Stdio::null())
                            .stdout(Stdio::piped())
                            .kill_on_drop(true)
                            .spawn()?;
                        let output =
                            tokio::time::timeout(Duration::from_secs(60), child.wait_with_output())
                                .await
                                .context("resource child timed out")??;
                        ensure!(
                            output.status.success(),
                            "resource child failed: {pattern}/{codec}/{buffer}/{large}"
                        );
                        let stdout = String::from_utf8(output.stdout)?;
                        ensure!(stdout.contains("1 passed; 0 failed; 0 ignored;"));
                        let rows: Vec<_> = stdout
                            .lines()
                            .filter_map(|line| line.strip_prefix("PACK_RESOURCE "))
                            .collect();
                        ensure!(rows.len() == 1, "expected one child measurement");
                        let mut row: serde_json::Value = serde_json::from_str(rows[0])?;
                        row["repetition"] = repetition.into();
                        println!("PACK_RESOURCE {row}");
                        fs::remove_dir_all(&case_root)?;
                        count += 1;
                    }
                }
            }
        }
    }
    ensure!(count == 48);
    Ok(())
}
