"""One-time cloud setup. Run it yourself:   .venv\\Scripts\\python cloud_setup.py

1. asks for your passphrase + YouTube Data API key (hidden input; nothing is printed or saved locally)
2. stores both as GitHub Actions secrets of this repo
3. encrypts your local database (history, subscriptions, watch data) and pushes it to the `data` branch
4. starts the first cloud run
Re-running it is safe: it re-uploads your current local database and updates the secrets.
"""
import getpass, json, shutil, sqlite3, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).parent


def sh(*cmd, input=None, cwd=None):
    r = subprocess.run(cmd, input=input, cwd=cwd, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"failed: {' '.join(cmd[:4])}…\n{r.stderr.strip()}")
    return r.stdout.strip()


def main():
    repo = sh("gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner", cwd=ROOT)
    print(f"Setting up cloud for {repo}\n")
    while True:
        pw = getpass.getpass("Choose a passphrase (encrypts your data; you'll also enter it on your phone): ")
        if len(pw) < 10:
            print("  use at least 10 characters"); continue
        if getpass.getpass("Type it again: ") == pw:
            break
        print("  didn't match, try again")
    key = getpass.getpass("YouTube Data API key (leave empty to add later): ").strip()

    sh("gh", "secret", "set", "VXP_PASSPHRASE", "--repo", repo, input=pw)
    if key:
        sh("gh", "secret", "set", "YT_API_KEY", "--repo", repo, input=key)
    print("✓ secrets stored in GitHub (encrypted; not readable back, even by you)")

    sys.path.insert(0, str(ROOT))
    import crypto_box
    tmp = Path(tempfile.mkdtemp())
    src = sqlite3.connect(ROOT / "videoxp.db"); dst = sqlite3.connect(tmp / "videoxp.db")
    src.backup(dst); dst.close(); src.close()                     # consistent copy even if the app is running
    work = tmp / "data"; (work / "events").mkdir(parents=True)
    (work / "events" / ".keep").write_text("")
    crypto_box.encrypt_file(tmp / "videoxp.db", work / "videoxp.db.enc", pw)
    (work / "README.md").write_text("Encrypted Video XP data (AES-256-GCM). Written by GitHub Actions; "
                                    "events/ holds encrypted watch data from the phone.\n")
    url = f"https://github.com/{repo}.git"
    sh("git", "init", "-q", "-b", "data", cwd=work)
    sh("git", "add", "-A", cwd=work)
    sh("git", "-c", "user.name=video-xp", "-c", "user.email=video-xp@users.noreply.github.com",
       "commit", "-q", "-m", "encrypted data", cwd=work)
    sh("git", "push", "-q", "-f", url, "data:data", cwd=work)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"✓ encrypted database uploaded ({(ROOT / 'videoxp.db').stat().st_size / 1e6:.0f} MB before compression)")

    sh("gh", "workflow", "run", "video-xp", "--repo", repo, "--ref", "main")
    owner, name = repo.split("/")
    print(f"""✓ first cloud run started (about 10–20 min): https://github.com/{repo}/actions

On your phone, open:  https://{owner.lower()}.github.io/{name}/
Then tap the screen → ⚙ and enter:
  • your passphrase
  • a GitHub token, made at https://github.com/settings/personal-access-tokens/new
      Repository access: Only select repositories → {name}
      Permissions → Contents: Read and write   (nothing else)
""")


if __name__ == "__main__":
    main()
