# Distributed Problem Data with JuiceFS

JuiceFS is a POSIX-compatible distributed filesystem backed by object
storage (R2) + metadata DB (Redis). Judges mount it as a local
directory — zero judge code changes needed.

```
Site + Judge hosts         Redis (metadata)       R2 (data)
  /home/ubuntu/problems  <------>  :6379/1  <------>  lqdoj-problem-data
     (FUSE mount)
```

## 1. R2 Bucket

Create a **private** R2 bucket (not the public media bucket):
- Cloudflare Dashboard → R2 → Create bucket → `lqdoj-problem-data`
- Do NOT enable public access
- Create API token with read/write access

## 2. Redis

JuiceFS metadata is rebuildable from S3 auto-backups (every 5 min),
so Redis can be kept simple. SQLite won't work (single-node only).

**Install and configure** (`/etc/redis/redis.conf`):

```bash
sudo apt install redis-server -y
```

```conf
bind 0.0.0.0                    # Listen on all interfaces
requirepass <strong-password>   # REQUIRED when binding 0.0.0.0
maxmemory 2gb
maxmemory-policy noeviction     # Never evict JuiceFS metadata
# appendonly yes                # Optional: survives restarts without S3 reload
```

```bash
sudo systemctl enable redis-server && sudo systemctl restart redis-server
redis-cli -a <password> PING    # Should return PONG
```

**Firewall** — restrict to known IPs:

```bash
sudo ufw allow from <site-ip> to any port 6379
sudo ufw allow from <judge-ip> to any port 6379
```

Metadata URL: `redis://:<password>@<redis-host>:6379/0`

**Alternative:** Reuse existing Django Redis with a different DB number
(e.g., `redis://:<password>@<redis-host>:6379/1`).

## 3. Install JuiceFS

On **site server** and all **judge hosts** (not inside Docker):

```bash
curl -sSL https://d.juicefs.com/install | sh -
```

## 4. Format (One Time)

```bash
juicefs format \
  --storage s3 \
  --bucket https://<ACCOUNT_ID>.r2.cloudflarestorage.com/lqdoj-problem-data \
  --access-key <R2_KEY> --secret-key <R2_SECRET> \
  "redis://:<password>@<redis-host>:6379/0" \
  lqdoj-problems
```

## 5. Mount (All Servers)

Add to `/etc/fstab` on **every machine** (site server + all judge hosts).
The site needs it to write problem data; judges need it to read.
Same line on all machines:

```
redis://:<password>@<redis-host>:6379/0  /home/ubuntu/problems  juicefs  _netdev,writeback,backup-meta=5m,cache-size=51200,open-cache=300,buffer-size=1024,allow_other  0  0
```

- `writeback`: writes to local cache first, uploads to R2 in background (keeps saves fast on site)
- `_netdev`: wait for network before mounting (boots before Docker)
- `backup-meta=5m`: auto-backup metadata to R2 every 5 minutes
- `allow_other`: lets Docker containers read the FUSE mount
- `cache-size=51200`: 50 GB local cache on SSD

Django doesn't know about JuiceFS or Redis — it just writes to
`/home/ubuntu/problems` like a normal directory. The fstab mount
handles all the R2/Redis plumbing at the OS level.

```bash
sudo mount /home/ubuntu/problems
df -h /home/ubuntu/problems   # Should show JuiceFS
```

## 6. Django Settings

```python
# dmoj/local_settings.py
DMOJ_PROBLEM_DATA_ROOT = "/home/ubuntu/problems"
DMOJ_PROBLEM_DATA_PUSH_UPDATE = True  # Bridge push (default)
```

## 7. Docker Judges

**Docker run** — two changes from your current command:
1. `:rshared` on the volume (required for FUSE mounts)
2. `--no-watchdog` flag (inotify doesn't work cross-client with FUSE)

```bash
sudo docker run \
  --name $judge_id \
  -v /home/ubuntu/problems:/problems:rshared \
  -d --network="host" \
  --cap-add=SYS_PTRACE \
  --restart=always \
  vnoj/judge-tierlqdoj:latest \
  run -c "$config_file" --no-watchdog 0.0.0.0 "$judge_id"
```

Boot order is handled by fstab `_netdev` (mounts before Docker restarts).

**Non-Docker:** Add `--no-watchdog` to your judge startup command.

## 8. Migrate Data

```bash
# If JuiceFS mounts at the same path as existing data:
mv /home/ubuntu/problems /home/ubuntu/problems-old
mkdir /home/ubuntu/problems
sudo mount /home/ubuntu/problems
rsync -av /home/ubuntu/problems-old/ /home/ubuntu/problems/
# Restart site + judges, then: rm -rf /home/ubuntu/problems-old
```

## 9. Verify

```bash
# Host sees data
ls /home/ubuntu/problems/<problem_code>/init.yml

# Container sees data
sudo docker exec $judge_id ls /problems/<problem_code>/init.yml

# Upload new test data on site → judge logs show "Updated problem list"
# Submit a solution → judged correctly
```

## Recovery (if Redis dies)

JuiceFS auto-backs up metadata to `meta/` in the R2 bucket every
5 minutes. To rebuild Redis:

```bash
# 1. Start fresh Redis (configure as in step 2)
# 2. Load latest backup from R2
juicefs load "redis://:<password>@<redis-host>:6379/0" \
  --storage s3 \
  --bucket https://<ACCOUNT_ID>.r2.cloudflarestorage.com/lqdoj-problem-data \
  --access-key <R2_KEY> --secret-key <R2_SECRET> \
  meta/dump-2026-03-15-120000.json.gz
# 3. Verify
juicefs fsck "redis://:<password>@<redis-host>:6379/0"
# 4. Remount: sudo mount /home/ubuntu/problems
```

Max data loss: 5 minutes (re-upload a few test cases at most).

## Troubleshooting

- **Mount status:** `juicefs status redis://:<password>@<redis-host>:6379/0`
- **Warm cache:** `juicefs warmup /home/ubuntu/problems/`
- **Judge not updating:** Check `--no-watchdog` in docker run command,
  check bridge connection in logs, or manual trigger:
  `curl -X POST http://judge-host:port/update/problems`
