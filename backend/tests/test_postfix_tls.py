"Public API."

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
POSTFIX_DIR = REPO_ROOT / "deploy" / "postfix"
TLS_SCRIPT = POSTFIX_DIR / "tls-setup.sh"
MAIN_CF = POSTFIX_DIR / "main.cf"
ENTRYPOINT = POSTFIX_DIR / "entrypoint.sh"
DOCKERFILE = POSTFIX_DIR / "Dockerfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"


def _run(tmp_path: Path, files: dict[str, str], *, postconf_rc: int = 0, mode: int = 0o600):
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    for name, content in files.items():
        (tls_dir / name).write_text(content)
        (tls_dir / name).chmod(mode if name == "privkey.pem" else 0o644)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "postconf.log"
    fake = bindir / "postconf"
    fake.write_text(
        f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a" >> "{log}"; done\nexit {postconf_rc}\n'
    )
    fake.chmod(0o755)
    env = {"PATH": f"{bindir}:{OPENSSL_DIR}:/usr/bin:/bin", "POSTFIX_TLS_DIR": str(tls_dir)}
    proc = subprocess.run(["bash", str(TLS_SCRIPT)], env=env, capture_output=True, text=True, timeout=20)
    args = log.read_text().splitlines() if log.exists() else []
    return proc, args, tls_dir


def _settings(args: list[str]) -> dict[str, str]:
    out = {}
    for arg in args:
        if "=" in arg and not arg.startswith("-"):
            key, value = arg.split("=", 1)
            out[key.strip()] = value.strip()
    return out


OPENSSL = shutil.which("openssl")
OPENSSL_DIR = str(Path(OPENSSL).parent) if OPENSSL else "/usr/bin"


def _pair(tmp_path_factory, days=30):
    d = tmp_path_factory.mktemp("pki")
    subprocess.run(
        [
            OPENSSL,
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            str(d / "k.pem"),
            "-out",
            str(d / "c.pem"),
            "-days",
            str(days),
            "-subj",
            "/CN=mx.test",
        ],
        check=True,
        capture_output=True,
    )
    return (d / "k.pem").read_text(), (d / "c.pem").read_text()


@pytest.fixture(scope="session")
def pki(tmp_path_factory):
    if not OPENSSL:
        pytest.skip("openssl not installed")
    key, cert = _pair(tmp_path_factory)
    other_key, _ = _pair(tmp_path_factory)
    return {"key": key, "cert": cert, "other_key": other_key}


@pytest.fixture
def CERTS(pki):  # noqa: N802 - kept the old constant's name for the existing tests
    return {"privkey.pem": pki["key"], "fullchain.pem": pki["cert"]}


def test_both_cert_files_enable_opportunistic_starttls(tmp_path, CERTS):
    proc, args, tls_dir = _run(tmp_path, CERTS)
    assert proc.returncode == 0, proc.stderr
    got = _settings(args)
    assert got["smtpd_tls_security_level"] == "may"
    assert got["smtpd_tls_chain_files"] == (f"{tls_dir}/privkey.pem, {tls_dir}/fullchain.pem")


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"privkey.pem": "KEY\n"},
        {"fullchain.pem": "CERT\n"},
        {"privkey.pem": "", "fullchain.pem": "CERT\n"},
        {"privkey.pem": "KEY\n", "fullchain.pem": ""},
    ],
    ids=["none", "key-only", "chain-only", "empty-key", "empty-chain"],
)
def test_missing_or_empty_cert_keeps_tls_off_and_logs_one_line(tmp_path, files):
    proc, args, _ = _run(tmp_path, files)
    assert proc.returncode == 0, proc.stderr
    got = _settings(args)
    assert got.get("smtpd_tls_security_level", "none") == "none"
    assert "smtpd_tls_chain_files" not in got or got["smtpd_tls_chain_files"] == ""
    lines = [ln for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    assert "STARTTLS disabled" in lines[0]


def test_unreadable_key_keeps_tls_off(tmp_path, CERTS):
    if os.geteuid() == 0:  # root reads any file regardless of mode; nothing to prove
        return
    proc, args, _ = _run(tmp_path, CERTS, mode=0)
    assert proc.returncode == 0
    assert _settings(args).get("smtpd_tls_security_level", "none") == "none"
    assert "STARTTLS disabled" in proc.stdout + proc.stderr


def test_postconf_failure_never_fails_the_container(tmp_path, CERTS):
    """A broken postconf must not stop Postfix from starting (mail > TLS)."""
    proc, _, _ = _run(tmp_path, CERTS, postconf_rc=1)
    assert proc.returncode == 0


def _main_cf() -> dict[str, str]:
    out = {}
    for line in MAIN_CF.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def test_main_cf_defaults_to_no_tls_and_pins_the_tls_policy():
    cf = _main_cf()
    # Fail-safe default: the entrypoint flips it to `may` only with a cert.
    assert cf["smtpd_tls_security_level"] == "none"
    assert cf["smtpd_tls_protocols"] == ">=TLSv1.2"
    assert cf["smtpd_tls_mandatory_protocols"] == ">=TLSv1.2"
    assert cf["smtpd_tls_loglevel"] == "1"
    assert cf["smtpd_tls_received_header"] == "yes"
    assert cf["tls_preempt_cipherlist"] == "no"
    assert cf["smtpd_tls_ciphers"] == "high"
    assert cf["smtpd_tls_mandatory_ciphers"] == "high"
    # Mozilla intermediate (TLS 1.2 suites; 1.3 suites are OpenSSL defaults).
    suites = cf["tls_high_cipherlist"].split(":")
    assert suites[:2] == ["ECDHE-ECDSA-AES128-GCM-SHA256", "ECDHE-RSA-AES128-GCM-SHA256"]
    assert all("CBC" not in s and "RC4" not in s and "3DES" not in s for s in suites)
    # Outbound client unchanged (still opportunistic, default medium grade).
    assert cf["smtp_tls_security_level"] == "may"
    assert "smtp_tls_ciphers" not in cf


def test_entrypoint_runs_tls_step_before_postfix_check():
    text = ENTRYPOINT.read_text()
    tls = text.find("/usr/local/libexec/mailsocket-postfix-tls")
    check = text.find("postfix check")
    assert 0 <= tls < check


def test_image_ships_the_tls_step():
    text = DOCKERFILE.read_text()
    assert re.search(r"COPY deploy/postfix/tls-setup.sh /usr/local/libexec/mailsocket-postfix-tls", text)
    assert "/usr/local/libexec/mailsocket-postfix-tls" in text.split("chmod 0755", 1)[1]


def test_compose_mounts_cert_dir_read_only():
    volumes = yaml.safe_load(COMPOSE.read_text())["services"]["postfix"]["volumes"]
    assert "./secrets/postfix-tls:/etc/postfix/tls:ro" in volumes


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("garbage_cert", "certificate unparseable or expired"),
        ("garbage_key", "private key unparseable"),
        ("mismatched_key", "private key does not match certificate"),
    ],
)
def test_invalid_cert_or_key_keeps_tls_off(tmp_path, pki, case, reason):
    files = {"privkey.pem": pki["key"], "fullchain.pem": pki["cert"]}
    if case == "garbage_cert":
        files["fullchain.pem"] = "CERT\n"
    elif case == "garbage_key":
        files["privkey.pem"] = "KEY\n"
    else:
        files["privkey.pem"] = pki["other_key"]
    proc, args, _ = _run(tmp_path, files)
    assert proc.returncode == 0
    assert _settings(args).get("smtpd_tls_security_level") == "none"
    assert "smtpd_tls_chain_files" not in _settings(args)
    assert proc.stderr.count("postfix-tls: STARTTLS disabled") == 1
    assert reason in proc.stderr


def test_missing_openssl_keeps_tls_off(tmp_path, CERTS, monkeypatch):
    """GPT round 2 P2: no openssl -> cannot validate -> stay plaintext."""
    global OPENSSL_DIR
    saved = OPENSSL_DIR
    empty = tmp_path / "noopenssl"
    empty.mkdir()
    for tool in ("bash", "sh", "cat", "printf", "test", "["):
        src = shutil.which(tool)
        if src:
            (empty / tool).symlink_to(src)
    OPENSSL_DIR = str(empty)
    try:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        for name, content in CERTS.items():
            (tls_dir / name).write_text(content)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        log = tmp_path / "postconf.log"
        (bindir / "postconf").write_text(
            f'#!{shutil.which("sh")}\nfor a in "$@"; do printf "%s\\n" "$a" >> "{log}"; done\n'
        )
        (bindir / "postconf").chmod(0o755)
        proc = subprocess.run(
            [shutil.which("bash"), str(TLS_SCRIPT)],
            env={"PATH": f"{bindir}:{empty}", "POSTFIX_TLS_DIR": str(tls_dir)},
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        OPENSSL_DIR = saved
    assert proc.returncode == 0
    assert _settings(log.read_text().splitlines()).get("smtpd_tls_security_level") == "none"
    assert "openssl not available" in proc.stderr
