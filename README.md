LQDOJ: Le Quy Don Online Judge
===

[![](https://github.com/DMOJ/online-judge/workflows/build/badge.svg)](https://lqdoj.edu.vn/)
[![Python](https://img.shields.io/pypi/pyversions/tensorflow.svg?style=plastic)](https://python.org)
[![OS](https://img.shields.io/badge/Ubuntu-16.04%20%7C%2018.04%20%7C%2020.04-brightgreen)](https://ubuntu.com/download)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](https://www.gnu.org/licenses/agpl-3.0.en.html)

# Overview

Homepage: [https://lqdoj.edu.vn](https://lqdoj.edu.vn)

Based on [DMOJ](https://dmoj.ca/).

**Supported Programming Languages:**

- Assembly (x64)
- AWK
- C
- C++03 / C++11 / C++14 / C++17 / C++20 / C++23
- Java
- Pascal
- Perl
- Python 2 / Python 3
- PyPy 2 / PyPy 3

**Features:**
- Plagiarism detection via [Stanford MOSS](https://theory.stanford.edu/~aiken/moss/)
- Contest management with various formats
- User rating and performance tracking
- Real-time chat system
- Multi-language support
- Dark/Light theme
- Organization management

# Installation

Most of the setup follows the same process as DMOJ installations. You can view the installation guide of DMOJ here: https://docs.dmoj.ca/#/site/installation.

The main difference is instead of `git clone https://github.com/DMOJ/site.git`, you clone this repository: `git clone https://github.com/LQDJudge/online-judge.git`.

## Prerequisites

**Step 1**: Install required libraries
- `$` here means sudo. For example, the first line means run `sudo apt update`

```bash
$ apt update
$ apt install git gcc g++ make python3-dev python3-pip libxml2-dev libxslt1-dev zlib1g-dev gettext curl redis-server pkg-config
$ curl -sL https://deb.nodesource.com/setup_18.x | sudo -E bash -
$ apt install nodejs
$ npm install -g sass postcss-cli postcss autoprefixer
```

**Step 2**: Set up database
- The server currently uses `MariaDB ≥ 10.5`, but you can also use `MySQL` if there are conflicts
- If after running the commands below your `MariaDB` version is old (< 10.5), you can Google how to install the latest `MariaDB` (10.5 or 10.6)
- You can check your `MariaDB` version by typing `sudo mysql` (Ctrl + C to quit)

```bash
$ apt update
$ apt install mariadb-server libmysqlclient-dev
```

**Step 3**: Create database tables
- You can change the table name and password

```bash
$ sudo mysql
mariadb> CREATE DATABASE dmoj DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_general_ci;
mariadb> GRANT ALL PRIVILEGES ON dmoj.* TO 'dmoj'@'localhost' IDENTIFIED BY '<password>';
mariadb> exit
$ mariadb-tzinfo-to-sql /usr/share/zoneinfo | sudo mariadb -u root mysql
```

**Step 4**: Set up virtual environment and pull code
- If `pip3 install mysqlclient` fails, try running `pip3 install mysqlclient==2.1.1`

```bash
$ python3 -m venv dmojsite
$ . dmojsite/bin/activate

$ git clone https://github.com/LQDJudge/online-judge.git
$ cd online-judge
$ git submodule init
$ git submodule update
$ pip3 install -r requirements.txt
$ pip3 install mysqlclient
$ pre-commit install
```

**Step 5**: Create `local_settings.py`
This is the file for customizing Django settings. Create the file at `online-judge/dmoj/local_settings.py`
- Sample file: `online-judge/dmoj/sample_local_settings.py`
- If you changed the database table name or password, update the corresponding information in `Databases`
- After completion, run `python3 manage.py check` to verify

**Step 6**: Compile CSS and translations
- Commands 1 and 2 should be run after each change to CSS or JS files (HTML files don't require this)
- Commands 3 and 4 should be run after each change to translation files
- Note: After running these commands, the folder corresponding to `STATIC_ROOT` in `local_settings.py` must be created. If it hasn't been created, you need to create that folder before running the first two commands.

```bash
$ ./make_style.sh
$ python3 manage.py collectstatic
$ python3 manage.py compilemessages
$ python3 manage.py compilejsi18n
```

**Step 7**: Add data to database

```bash
$ python3 manage.py migrate
$ python3 manage.py loaddata navbar
$ python3 manage.py loaddata language_small
$ python3 manage.py loaddata demo
```

**Step 8**: Run the site
At this point, the basic setup is complete (without judge, websocket, celery). You can access it at `localhost:8000`

```bash
python3 manage.py runserver 0.0.0.0:8000
```

## Important Notes

1. **(WSL)** You can download the Terminal application from the Windows Store
2. **(WSL)** Each time you open Ubuntu, you need to run the following command to start MariaDB: `sudo service mysql restart` (similar for other services like memcached, celery)
3. After installation, you only need to activate the virtual environment and run the runserver command:

    ```bash
    . dmojsite/bin/activate
    python3 manage.py runserver
    ```

4. For nginx, after configuring according to the DMOJ guide, you need to add the following location to use the profile image feature. Replace `path/to/oj` with the path where you cloned the source code:

    ```
    location /profile_images/ {
        root /path/to/oj;
    }
    ```

5. Development workflow:
    1. After changing code, Django will automatically rebuild, you just need to press F5
    2. Some styles are in .scss files. You need to recompile CSS to see changes.

## Testing

### Setup Test Database
Before running unit tests, create the test database in MariaDB/MySQL:

```bash
sudo mariadb
```

```sql
CREATE DATABASE test_dmoj DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_general_ci;
GRANT ALL PRIVILEGES ON test_dmoj.* TO 'dmoj'@'localhost';
FLUSH PRIVILEGES;
```

### Run Tests

```bash
# Run all tests
python3 manage.py test judge.tests --keepdb

# Run a specific test
python3 manage.py test judge.tests.TestClass.test_method --keepdb
```

## Optional Components

### Useful Aliases

You can save these aliases for faster development:

- `mtrans`: to generate translation when you add a string in code
- `trans`: compile translation (after you've translated to Vietnamese)
- `cr`: change to OJ folder
- `pr`: run server
- `sm`: restart services (mainly for WSL)
- `sd`: activate virtual env
- `css`: compile CSS files

```bash
alias mtrans='python3 manage.py makemessages -l vi && python3 manage.py makedmojmessages -l vi --no-mark-obsolete'
alias pr='python3 manage.py runserver'
alias sd='source ~/LQDOJ/dmojsite/bin/activate'
alias sm='sudo service mysql restart && sudo service redis-server start && sudo service memcached start'
alias trans='python3 manage.py compilemessages -l vi && python3 manage.py compilejsi18n -l vi'
alias cr='cd ~/LQDOJ/online-judge'
alias css='./make_style.sh && python3 manage.py collectstatic --noinput'
```

### Memcached

Used for in-memory caching:

```bash
$ sudo apt install memcached
```

### WebSocket

Used for live updates (like chat):

- The WebSocket configuration file `online-judge/websocket/config.js` is already included with default settings.

- Install Node.js dependencies:

```bash
$ cd websocket
$ npm install
```

- Add WebSocket settings to `local_settings.py`:

```python
# WebSocket daemon settings
EVENT_DAEMON_KEY = 'lqdoj'  # Must match backend_auth_token in config.js
EVENT_DAEMON_URL = 'http://127.0.0.1:15100'
EVENT_DAEMON_PUBLIC_URL = 'http://127.0.0.1:15100'  # Same as EVENT_DAEMON_URL in development

# For production with SSL/domain
# EVENT_DAEMON_PUBLIC_URL = 'wss://your-domain.com'  # nginx proxies to port 15100
```

- Start (in a separate tab)

```bash
$ node websocket/daemon.js
```

**Production Deployment:**

For nginx, add this location block:

```nginx
location /socket.io/ {
    proxy_pass http://127.0.0.1:15100/socket.io/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400;
}
```

### Celery

Used for background tasks like batch rejudging:

```bash
celery -A dmoj_celery worker
```

### Judge

Install in a separate folder outside the site:

```bash
$ apt install python3-dev python3-pip build-essential libseccomp-dev
$ git clone https://github.com/LQDJudge/judge-server.git
$ cd judge-server
$ sudo pip3 install -e .
```

- Create a `judge.yml` file outside the judge-server folder (sample file: https://github.com/DMOJ/docs/blob/master/sample_files/judge_conf.yml)
- Add judge to site via UI: Admin → Judge → Add Judge → enter id and key (only need to add once) or use command `python3 manage.py addjudge <id> <key>`
- Run Bridge (connection between judge and site) in a separate tab in the online-judge folder:

```bash
$ python3 manage.py runbridged
```

- Start Judge (in a separate tab):

```bash
$ dmoj -c judge.yml localhost
```

- Note: Each time you want to run judges later, open 1 tab for bridge and n tabs for judges. Each judge needs a different yml file (containing different authentication)

### Distributed Judges (JuiceFS)

For running judges on multiple servers, you can use [JuiceFS](https://juicefs.com/) to share problem data across machines via a POSIX-compatible distributed filesystem backed by S3/R2. See [docs/juicefs-setup.md](docs/juicefs-setup.md) for the full setup guide.

## Common Installation Issues

1. **Missing `local_settings.py`**: You need to copy the `local_settings.py` in order to pass the check.
2. **Missing problem folder in `local_settings.py`**: You need to create a folder to contain all problem packages and configure in `local_settings.py`.
3. **Missing static folder in `local_settings.py`**: Similar to problem folder, make sure to configure `STATIC_FILES` inside `local_settings.py`.
4. **Missing configure file for judges**: Each judge must have a separate configure file. To create this file, you can run `dmoj-autoconf`. Check out all sample files here: https://github.com/DMOJ/docs/blob/master/sample_files.
5. **Missing timezone data for SQL**: If you're using Ubuntu and following DMOJ's installation guide for the server, and you get the error mentioned in https://github.com/LQDJudge/online-judge/issues/45, then you can follow this method to fix:

    ```bash
    # You may have to do this if you haven't set root password for MySQL, replace mypass with your password
    # SET PASSWORD FOR 'root'@'localhost' = PASSWORD('mypass');
    # FLUSH PRIVILEGES;
    mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql -D mysql -u root -p 
    mysql -u root -p -e "flush tables;" mysql
    ```

6. **Missing chat secret key**: You must generate a Fernet key, and assign a variable in `local_settings.py` like this:

    ```python
    CHAT_SECRET_KEY = "81HqDtbqAywKSOumSxxxxxxxxxxxxxxxxx="
    ```

# Usage

After finishing all installation steps, every time you want to run a local server, follow these steps:

1. **Activate virtual environment:**

    ```bash
    source dmojsite/bin/activate
    ```

2. **Run server:**

    ```bash
    python3 manage.py runserver 0.0.0.0:8000
    ```

3. **Create a bridge** (open in a different terminal if using the same machine)

    ```bash
    python3 manage.py runbridged
    ```

4. **Create a judge** (another terminal)

    ```bash
    dmoj 0.0.0.0 -p 9999 -c <path to yml configure file>
    ```

   Here we assume you use the default port `9999` for bridge in `settings.py`. You can create multiple judges, each should be in a separate terminal.

### Optional Services

1. **Run celery worker** (server's queue, may be necessary for some functions)

    ```bash
    celery -A dmoj_celery worker
    ```

2. **Run live event server** (for real-time updates like in production)

    ```bash
    node websocket/daemon.js
    ```

3. **To use subdomain for each organization**: Go to admin page → navigation bar → sites, add domain name (e.g., `localhost:8000`). Then add `USE_SUBDOMAIN = True` to `local_settings.py`.

# Development & Deployment

Most steps are similar to standard Django tutorials. Here are two common operations:

## 1. Update Translations

- If you add any new phrases in the code: `python3 manage.py makemessages`
- Go to `locale/vi`
- Modify `.po` file
- `python3 manage.py compilemessages`
- `python3 manage.py compilejsi18n`

## 2. Update Styles (using SASS)

- Change `.css/.scss` files in `resources` folder
- `./make_style.sh && python3 manage.py collectstatic`
- Sometimes you need to press `Ctrl + F5` to see the new user interface in browser

# Production Deployment

This section covers deploying LQDOJ to a production server. The setup uses **Nginx** as a reverse proxy, **uWSGI** as the application server, **Supervisor** to manage processes, and **Docker** for the bridge and judges.

It is assumed you have completed the [Installation](#installation) steps (database, virtualenv, code, migrations, static files) on your production server, and that services like Memcached, Redis, Celery, and WebSocket are configured as described in [Optional Components](#optional-components).

## Pre-deployment Configuration

Edit `local_settings.py` for production:

```python
# SECURITY: disable debug mode
DEBUG = False

# Generate a strong secret key:
# python3 -c 'from django.core.management.utils import get_random_secret_key;print(get_random_secret_key())'
SECRET_KEY = '<your generated secret key>'

# Optional: SSL settings (uncomment if using HTTPS)
# DMOJ_SSL = 2
# SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
# SECURE_SSL_REDIRECT = True
# SESSION_COOKIE_SECURE = True
# CSRF_COOKIE_SECURE = True

# Database - use 127.0.0.1 (not localhost) to force TCP connection.
# This is required for the Docker bridge to connect to the database.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'dmoj',
        'USER': 'dmoj',
        'PASSWORD': '<your password>',
        'HOST': '127.0.0.1',
        'OPTIONS': {
            'charset': 'utf8mb4',
            'sql_mode': 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION',
        },
    },
}

# Bridge - bind to 0.0.0.0 so Docker judges can connect
BRIDGED_JUDGE_ADDRESS = [('0.0.0.0', 9999)]
BRIDGED_DJANGO_ADDRESS = [('localhost', 9998)]
```

Make sure the required services are installed and running:

```bash
sudo apt install memcached redis-server
sudo systemctl enable memcached redis-server
sudo systemctl start memcached redis-server
```

## uWSGI

Install uWSGI inside the virtualenv:

```bash
(dmojsite) $ pip3 install uwsgi
```

Copy [`sample_conf/uwsgi.ini`](sample_conf/uwsgi.ini) to the site root directory and adjust the paths. Test with:

```bash
(dmojsite) $ uwsgi --ini uwsgi.ini
```

## Supervisor

Install Supervisor:

```bash
sudo apt install supervisor
```

Copy the sample configs from [`sample_conf/supervisor/`](sample_conf/supervisor/) to `/etc/supervisor/conf.d/` and adjust the paths:

- **`site.conf`** — Django application server (uWSGI)
- **`celery.conf`** — Background task worker
- **`wsevent.conf`** — WebSocket event server

Then load and start all services:

```bash
sudo supervisorctl update
sudo supervisorctl status
```

## Nginx

Install Nginx:

```bash
sudo apt install nginx
```

Copy [`sample_conf/nginx/nginx.conf`](sample_conf/nginx/nginx.conf) to `/etc/nginx/conf.d/` and adjust the paths. Then test and reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

If you get 403 errors on static files, nginx (running as `www-data`) likely can't traverse your home directory. Fix with:

```bash
chmod o+x /home/<user> /path/to/static /path/to/media
```

### SSL with Let's Encrypt (Optional)

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d <your domain>
```

Certbot will automatically modify the Nginx config to add SSL. If you enable SSL, uncomment the SSL settings in `local_settings.py` (see [Pre-deployment Configuration](#pre-deployment-configuration)).

## Bridge (Docker)

LQDOJ runs the bridge in Docker (instead of Supervisor as in standard DMOJ). The Docker files are in [`.docker/bridge/`](.docker/bridge/).

Build the bridge image (run from the parent of the `online-judge` directory):

```bash
.docker/bridge/build.sh
```

Start the bridge:

```bash
.docker/bridge/run.sh
```

The bridge uses `--network=host` so it can connect to the host's database on `127.0.0.1` and be reachable by judges on port 9999.

Verify the bridge is running:

```bash
docker logs bridge
```

## Judge Setup (Docker)

LQDOJ uses Docker-based judges built from the [DMOJ judge-server](https://github.com/DMOJ/judge-server). Scripts are in [`.docker/judge/`](.docker/judge/).

### Build the Judge Image

Clone the judge-server repo alongside the online-judge directory (if not already done):

```bash
git clone https://github.com/LQDJudge/judge-server.git
```

Then build the Docker image:

```bash
.docker/judge/build_image.sh
```

By default the script looks for `judge-server/` next to `online-judge/`. Set `JUDGE_SERVER_DIR` to override.

> **Tip:** The full `tierlqdoj` image includes all runtimes and takes a long time to build. For a quick start, you can build `tier1` instead (Python 2/3, C/C++, Java 8, Pascal):
> ```bash
> cd judge-server/.docker && make judge-tier1
> ```
> Then set `JUDGE_IMAGE=vnoj/judge-tier1:latest` when running judges.

### Register Judges

Register judges in the site database (run from the `online-judge` directory with virtualenv activated):

```bash
.docker/judge/register_judges.sh 1 10 '<authentication key>'
```

This registers `judge1` through `judge10` with the given key. You can also add judges via the admin panel: **Admin** -> **Judges** -> **Add Judge**.

### Judge Configuration

Create a config file in your problems directory (e.g., `/mnt/problems/__conf__/general.yml`). A full sample is at [`sample_conf/judge.yml`](sample_conf/judge.yml). The judge name is passed via the command line, so the config only needs the key and paths:

```yaml
key: '<authentication key>'
problem_storage_globs:
  - /problems/**/
# All configuration for language executors.
# If you're unsure of what values a language needs, consult the source of the executor:
# <https://github.com/DMOJ/judge/tree/master/dmoj/executors>
runtime:
  g++: /usr/bin/g++
  gcc: /usr/bin/gcc
  fpc: /usr/bin/fpc
  java: /usr/lib/jvm/java-25-openjdk-amd64/bin/java
  javac: /usr/lib/jvm/java-25-openjdk-amd64/bin/javac
  python3: /usr/bin/python3
  pypy3: /opt/pypy3/bin/pypy3
  # ... add more runtimes as needed
```

Note: the `problem_storage_globs` uses `/problems/` because that's the mount point inside the Docker container.

### Start Judges

Set `PROBLEMS_DIR` to your problems directory before running judges:

```bash
export PROBLEMS_DIR=/path/to/problems
```

Start a single judge:

```bash
.docker/judge/start_judge.sh judge1
```

Start multiple judges at once:

```bash
.docker/judge/start_judges.sh 1 5
```

This starts `judge1` through `judge5`. Each judge runs in its own Docker container with `--network=host`.

Verify judges are connected in **Admin** -> **Judges** — they should appear as online.

### Judges on Separate Servers

Make sure port 9999 is open on the site server's firewall. For sharing problem data across multiple servers, see [Distributed Judges (JuiceFS)](#distributed-judges-juicefs).

## Optional: S3 Storage for Media

By default, media files (user uploads, profile images, etc.) are stored on the local filesystem. To use Amazon S3 or S3-compatible storage (e.g., Cloudflare R2) instead:

```bash
pip install django-storages[boto3]
```

Then uncomment and configure the S3 section in `local_settings.py` (see `sample_local_settings.py` for all options):

```python
AWS_ACCESS_KEY_ID = 'your-access-key'
AWS_SECRET_ACCESS_KEY = 'your-secret-key'
AWS_STORAGE_BUCKET_NAME = 'your-bucket-name'
AWS_S3_REGION_NAME = 'ap-southeast-1'
AWS_S3_CUSTOM_DOMAIN = 'cdn.example.com'  # Optional: CloudFront or custom domain
DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
```

No code changes are needed — the codebase uses Django's `default_storage` abstraction, so all file operations automatically use S3 when configured.

## Optional: ML Recommendations (Vector Search)

LQDOJ includes a problem recommendation system using collaborative filtering and neural Two Tower models with MariaDB vector search. Requires MariaDB 11.7+.

See [`judge/ml/README.md`](judge/ml/README.md) for the full setup guide.

## Crontab

Recommended cron jobs for production. Edit with `crontab -e` and replace `<venv>` and `<site>` with your actual paths:

```crontab
0 4 * * * <venv>/bin/python3 <site>/manage.py cleanup_inactive --users --orgs
4 4 * * * <venv>/bin/python3 <site>/manage.py batch_clearsessions
7 4 * * * <venv>/bin/python3 <site>/manage.py recompute_comment_scores
10 4 * * * <venv>/bin/python3 <site>/manage.py delete_old_notifications
11 4 * * * <venv>/bin/python3 <site>/manage.py recompute_contributions
15 4 * * * <venv>/bin/python3 <site>/manage.py fix_organization_private
```

---

# Phiên bản Tiếng Việt

<details>
<summary>Nhấp để xem hướng dẫn bằng Tiếng Việt</summary>

## Tổng quan

Trang chủ: [https://lqdoj.edu.vn](https://lqdoj.edu.vn)

Dựa trên [DMOJ](https://dmoj.ca/).

**Ngôn ngữ lập trình được hỗ trợ:**

- Assembly (x64)
- AWK
- C
- C++03 / C++11 / C++14 / C++17 / C++20 / C++23
- Java
- Pascal
- Perl
- Python 2 / Python 3
- PyPy 2 / PyPy 3

**Tính năng:**
- Phát hiện đạo văn qua [Stanford MOSS](https://theory.stanford.edu/~aiken/moss/)
- Quản lý cuộc thi với nhiều định dạng khác nhau
- Theo dõi rating và hiệu suất người dùng
- Hệ thống chat thời gian thực
- Hỗ trợ đa ngôn ngữ
- Giao diện sáng/tối
- Quản lý tổ chức

## Cài đặt

Hầu hết các bước cài đặt giống với DMOJ. Bạn có thể xem hướng dẫn cài đặt DMOJ tại: https://docs.dmoj.ca/#/site/installation.

Điểm khác biệt chính là thay vì `git clone https://github.com/DMOJ/site.git`, bạn clone repository này: `git clone https://github.com/LQDJudge/online-judge.git`.

### Yêu cầu hệ thống

**Bước 1**: Cài các thư viện cần thiết
- `$` ở đây nghĩa là sudo. Ví dụ dòng đầu nghĩa là chạy lệnh `sudo apt update`

```bash
$ apt update
$ apt install git gcc g++ make python3-dev python3-pip libxml2-dev libxslt1-dev zlib1g-dev gettext curl redis-server pkg-config
$ curl -sL https://deb.nodesource.com/setup_18.x | sudo -E bash -
$ apt install nodejs
$ npm install -g sass postcss-cli postcss autoprefixer
```

**Bước 2**: Tạo cơ sở dữ liệu
- Server đang dùng `MariaDB ≥ 10.5`, các bạn cũng có thể dùng `MySQL` nếu bị conflict
- Nếu sau khi chạy lệnh dưới mà version `MariaDB` bị cũ (< 10.5) thì có thể tra Google cách cài `MariaDB` mới nhất (10.5 hoặc 10.6)
- Các bạn có thể thấy version `MariaDB` bằng cách gõ lệnh `sudo mysql` (Ctrl + C để quit)

```bash
$ apt update
$ apt install mariadb-server libmysqlclient-dev
```

**Bước 3**: Tạo bảng trong cơ sở dữ liệu
- Các bạn có thể thay tên bảng và mật khẩu

```bash
$ sudo mysql
mariadb> CREATE DATABASE dmoj DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_general_ci;
mariadb> GRANT ALL PRIVILEGES ON dmoj.* TO 'dmoj'@'localhost' IDENTIFIED BY '<password>';
mariadb> exit
$ mariadb-tzinfo-to-sql /usr/share/zoneinfo | sudo mariadb -u root mysql
```

**Bước 4**: Cài đặt môi trường ảo (virtual env) và pull code
- Nếu `pip3 install mysqlclient` bị lỗi thì thử chạy `pip3 install mysqlclient==2.1.1`

```bash
$ python3 -m venv dmojsite
$ . dmojsite/bin/activate

$ git clone https://github.com/LQDJudge/online-judge.git
$ cd online-judge
$ git submodule init
$ git submodule update
$ pip3 install -r requirements.txt
$ pip3 install mysqlclient
$ pre-commit install
```

**Bước 5**: Tạo `local_settings.py`
Đây là file để custom setting cho Django. Các bạn tạo file vào `online-judge/dmoj/local_settings.py`
- File mẫu: `online-judge/dmoj/sample_local_settings.py`
- Nếu bạn đổi tên hoặc mật khẩu bảng cơ sở dữ liệu thì thay đổi thông tin tương ứng trong `Databases`
- Sau khi xong, chạy lệnh `python3 manage.py check` để kiểm tra

**Bước 6**: Compile CSS và bản dịch
- Lệnh 1 và 2 gọi sau mỗi lần thay đổi file CSS hoặc file JS (file HTML thì không cần)
- Lệnh 3 và 4 gọi sau mỗi lần thay đổi file dịch
- Note: Sau khi chạy lệnh này, thư mục tương ứng với `STATIC_ROOT` trong `local_settings.py` phải được tạo. Nếu chưa được tạo thì cần tạo thư mục đó trước khi chạy 2 lệnh đầu.

```bash
$ ./make_style.sh
$ python3 manage.py collectstatic
$ python3 manage.py compilemessages
$ python3 manage.py compilejsi18n
```

**Bước 7**: Thêm dữ liệu vào cơ sở dữ liệu

```bash
$ python3 manage.py migrate
$ python3 manage.py loaddata navbar
$ python3 manage.py loaddata language_small
$ python3 manage.py loaddata demo
```

**Bước 8**: Chạy trang web
Đến đây thì cơ bản đã hoàn thành (chưa có judge, websocket, celery). Các bạn có thể truy cập tại `localhost:8000`

```bash
python3 manage.py runserver 0.0.0.0:8000
```

### Một số lưu ý quan trọng

1. **(WSL)** Có thể tải ứng dụng Terminal trong Windows Store
2. **(WSL)** Mỗi lần mở Ubuntu, các bạn cần chạy lệnh sau để MariaDB khởi động: `sudo service mysql restart` (tương tự cho một số service khác như memcached, celery)
3. Sau khi cài đặt, các bạn chỉ cần activate virtual env và chạy lệnh runserver:

    ```bash
    . dmojsite/bin/activate
    python3 manage.py runserver
    ```

4. Đối với nginx, sau khi config xong theo guide của DMOJ, bạn cần thêm location như sau để sử dụng được tính năng profile image, thay thế `path/to/oj` thành đường dẫn nơi bạn đã clone source code:

    ```
    location /profile_images/ {
        root /path/to/oj;
    }
    ```

5. Quy trình phát triển:
    1. Sau khi thay đổi code thì Django tự build lại, các bạn chỉ cần F5
    2. Một số style nằm trong các file .scss. Các bạn cần recompile CSS thì mới thấy được thay đổi.

## Kiểm thử

### Tạo database test
Trước khi chạy unit test, tạo database test trong MariaDB/MySQL:

```bash
sudo mariadb
```

```sql
CREATE DATABASE test_dmoj DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_general_ci;
GRANT ALL PRIVILEGES ON test_dmoj.* TO 'dmoj'@'localhost';
FLUSH PRIVILEGES;
```

### Chạy test

```bash
# Chạy tất cả test
python3 manage.py test judge.tests --keepdb

# Chạy một test cụ thể
python3 manage.py test judge.tests.TestClass.test_method --keepdb
```

## Các thành phần tùy chọn

### Alias hữu ích

Các bạn có thể lưu các alias này để sau này dùng cho nhanh:

```bash
alias mtrans='python3 manage.py makemessages -l vi && python3 manage.py makedmojmessages -l vi --no-mark-obsolete'
alias pr='python3 manage.py runserver'
alias sd='source ~/LQDOJ/dmojsite/bin/activate'
alias sm='sudo service mysql restart && sudo service redis-server start && sudo service memcached start'
alias trans='python3 manage.py compilemessages -l vi && python3 manage.py compilejsi18n -l vi'
alias cr='cd ~/LQDOJ/online-judge'
alias css='./make_style.sh && python3 manage.py collectstatic --noinput'
```

### Memcached

Dùng cho in-memory cache:

```bash
$ sudo apt install memcached
```

### WebSocket

Dùng để live update (như chat):

- File cấu hình WebSocket `online-judge/websocket/config.js` đã được bao gồm với cài đặt mặc định.

- Cài các thư viện Node.js:

```bash
$ cd websocket
$ npm install
```

- Thêm cài đặt WebSocket vào `local_settings.py`:

```python
# WebSocket daemon settings
EVENT_DAEMON_KEY = 'lqdoj'  # Phải giống backend_auth_token trong config.js
EVENT_DAEMON_URL = 'http://127.0.0.1:15100'
EVENT_DAEMON_PUBLIC_URL = 'http://127.0.0.1:15100'  # Giống EVENT_DAEMON_URL trong development

# Cho production với SSL/domain
# EVENT_DAEMON_PUBLIC_URL = 'wss://your-domain.com'  # nginx proxy đến port 15100
```

- Khởi động (trong 1 tab riêng)

```bash
$ node websocket/daemon.js
```

**Triển khai Production:**

Cho nginx, thêm location block này:

```nginx
location /socket.io/ {
    proxy_pass http://127.0.0.1:15100/socket.io/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400;
}
```

### Celery

Dùng cho một số task như batch rejudge:

```bash
celery -A dmoj_celery worker
```

### Judge

Cài đặt ở 1 thư mục riêng bên ngoài site:

```bash
$ apt install python3-dev python3-pip build-essential libseccomp-dev
$ git clone https://github.com/LQDJudge/judge-server.git
$ cd judge-server
$ sudo pip3 install -e .
```

- Tạo một file `judge.yml` ở bên ngoài thư mục judge-server (file mẫu: https://github.com/DMOJ/docs/blob/master/sample_files/judge_conf.yml)
- Thêm judge vào site bằng UI: Admin → Judge → Thêm Judge → nhập id và key (chỉ cần thêm 1 lần) hoặc dùng lệnh `python3 manage.py addjudge <id> <key>`
- Chạy Bridge (cầu nối giữa judge và site) trong 1 tab riêng trong thư mục online-judge:

```bash
$ python3 manage.py runbridged
```

- Khởi động Judge (trong 1 tab riêng):

```bash
$ dmoj -c judge.yml localhost
```

- Lưu ý: Mỗi lần sau này muốn chạy judge thì mở 1 tab cho bridge và n tab cho judge. Mỗi judge cần 1 file yml khác nhau (chứa authentication khác nhau)

### Judge phân tán (JuiceFS)

Để chạy judge trên nhiều server, bạn có thể dùng [JuiceFS](https://juicefs.com/) để chia sẻ dữ liệu bài tập qua hệ thống file phân tán tương thích POSIX, backed bởi S3/R2. Xem [docs/juicefs-setup.md](docs/juicefs-setup.md) để biết chi tiết.

## Các lỗi cài đặt thường gặp

1. **Thiếu `local_settings.py`**: Bạn cần tạo file `local_settings.py` để pass được bước check.
2. **Thiếu thư mục problem trong `local_settings.py`**: Bạn cần tạo thư mục chứa các gói bài tập và cấu hình trong `local_settings.py`.
3. **Thiếu thư mục static trong `local_settings.py`**: Tương tự thư mục problem, đảm bảo cấu hình `STATIC_FILES` trong `local_settings.py`.
4. **Thiếu file cấu hình cho judge**: Mỗi judge cần một file cấu hình riêng. Để tạo file này, bạn có thể chạy `dmoj-autoconf`. Xem tất cả file mẫu tại: https://github.com/DMOJ/docs/blob/master/sample_files.
5. **Thiếu dữ liệu timezone cho SQL**: Nếu bạn dùng Ubuntu và làm theo hướng dẫn cài đặt của DMOJ, gặp lỗi như trong https://github.com/LQDJudge/online-judge/issues/45, có thể sửa bằng cách:

    ```bash
    # Có thể cần đặt mật khẩu root cho MySQL, thay mypass bằng mật khẩu của bạn
    # SET PASSWORD FOR 'root'@'localhost' = PASSWORD('mypass');
    # FLUSH PRIVILEGES;
    mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql -D mysql -u root -p
    mysql -u root -p -e "flush tables;" mysql
    ```

6. **Thiếu chat secret key**: Bạn cần tạo Fernet key và gán biến trong `local_settings.py`:

    ```python
    CHAT_SECRET_KEY = "81HqDtbqAywKSOumSxxxxxxxxxxxxxxxxx="
    ```

## Sử dụng

Sau khi hoàn thành cài đặt, mỗi lần muốn chạy server local, làm theo các bước sau:

1. **Activate virtual environment:**

    ```bash
    source dmojsite/bin/activate
    ```

2. **Chạy server:**

    ```bash
    python3 manage.py runserver 0.0.0.0:8000
    ```

3. **Chạy bridge** (mở terminal khác nếu cùng máy)

    ```bash
    python3 manage.py runbridged
    ```

4. **Chạy judge** (terminal khác)

    ```bash
    dmoj 0.0.0.0 -p 9999 -c <đường dẫn đến file yml>
    ```

   Ở đây giả sử bạn dùng port mặc định `9999` cho bridge trong `settings.py`. Bạn có thể tạo nhiều judge, mỗi judge chạy trong một terminal riêng.

### Dịch vụ tùy chọn

1. **Chạy celery worker** (hàng đợi tác vụ, cần thiết cho một số chức năng)

    ```bash
    celery -A dmoj_celery worker
    ```

2. **Chạy live event server** (cho cập nhật real-time)

    ```bash
    node websocket/daemon.js
    ```

3. **Sử dụng subdomain cho tổ chức**: Vào trang admin → navigation bar → sites, thêm tên miền (ví dụ `localhost:8000`). Sau đó thêm `USE_SUBDOMAIN = True` vào `local_settings.py`.

## Phát triển

Hầu hết các bước tương tự Django tutorials chuẩn. Dưới đây là hai thao tác thường dùng:

### 1. Cập nhật bản dịch

- Nếu bạn thêm chuỗi mới trong code: `python3 manage.py makemessages`
- Vào `locale/vi`
- Sửa file `.po`
- `python3 manage.py compilemessages`
- `python3 manage.py compilejsi18n`

### 2. Cập nhật giao diện (SASS)

- Thay đổi file `.css/.scss` trong thư mục `resources`
- `./make_style.sh && python3 manage.py collectstatic`
- Đôi khi cần nhấn `Ctrl + F5` để thấy giao diện mới trong trình duyệt

## Triển khai Production

Phần này hướng dẫn triển khai LQDOJ lên server production. Hệ thống sử dụng **Nginx** làm reverse proxy, **uWSGI** làm application server, **Supervisor** để quản lý tiến trình, và **Docker** cho bridge và judge.

Giả sử bạn đã hoàn thành các bước [Cài đặt](#cài-đặt) (database, virtualenv, code, migration, static files) trên server production, và các dịch vụ như Memcached, Redis, Celery, WebSocket đã được cấu hình như mô tả trong [Các thành phần tùy chọn](#các-thành-phần-tùy-chọn).

### Cấu hình trước triển khai

Chỉnh sửa `local_settings.py` cho production:

```python
# BẢO MẬT: tắt chế độ debug
DEBUG = False

# Tạo secret key mạnh:
# python3 -c 'from django.core.management.utils import get_random_secret_key;print(get_random_secret_key())'
SECRET_KEY = '<secret key của bạn>'

# Tùy chọn: cài đặt SSL (bỏ comment nếu dùng HTTPS)
# DMOJ_SSL = 2
# SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
# SECURE_SSL_REDIRECT = True
# SESSION_COOKIE_SECURE = True
# CSRF_COOKIE_SECURE = True

# Database - dùng 127.0.0.1 (không dùng localhost) để buộc kết nối TCP.
# Cần thiết để Docker bridge kết nối được đến database.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'dmoj',
        'USER': 'dmoj',
        'PASSWORD': '<mật khẩu>',
        'HOST': '127.0.0.1',
        'OPTIONS': {
            'charset': 'utf8mb4',
            'sql_mode': 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION',
        },
    },
}

# Bridge - bind 0.0.0.0 để Docker judge có thể kết nối
BRIDGED_JUDGE_ADDRESS = [('0.0.0.0', 9999)]
BRIDGED_DJANGO_ADDRESS = [('localhost', 9998)]
```

Đảm bảo các dịch vụ cần thiết đã được cài và chạy:

```bash
sudo apt install memcached redis-server
sudo systemctl enable memcached redis-server
sudo systemctl start memcached redis-server
```

### uWSGI

Cài uWSGI trong virtualenv:

```bash
(dmojsite) $ pip3 install uwsgi
```

Copy [`sample_conf/uwsgi.ini`](sample_conf/uwsgi.ini) vào thư mục gốc của site và chỉnh sửa đường dẫn. Test bằng:

```bash
(dmojsite) $ uwsgi --ini uwsgi.ini
```

### Supervisor

Cài Supervisor:

```bash
sudo apt install supervisor
```

Copy các file cấu hình mẫu từ [`sample_conf/supervisor/`](sample_conf/supervisor/) vào `/etc/supervisor/conf.d/` và chỉnh sửa đường dẫn:

- **`site.conf`** — Django application server (uWSGI)
- **`celery.conf`** — Worker xử lý tác vụ nền
- **`wsevent.conf`** — WebSocket event server

Sau đó load và khởi động:

```bash
sudo supervisorctl update
sudo supervisorctl status
```

### Nginx

Cài Nginx:

```bash
sudo apt install nginx
```

Copy [`sample_conf/nginx/nginx.conf`](sample_conf/nginx/nginx.conf) vào `/etc/nginx/conf.d/` và chỉnh sửa đường dẫn. Sau đó test và reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Nếu gặp lỗi 403 với static files, nginx (chạy dưới user `www-data`) có thể không đọc được thư mục home. Sửa bằng:

```bash
chmod o+x /home/<user> /path/to/static /path/to/media
```

#### SSL với Let's Encrypt (Tùy chọn)

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d <domain của bạn>
```

Certbot sẽ tự động chỉnh sửa cấu hình Nginx để thêm SSL. Nếu bật SSL, bỏ comment các cài đặt SSL trong `local_settings.py`.

### Bridge (Docker)

LQDOJ chạy bridge trong Docker (thay vì Supervisor như DMOJ chuẩn). Các file Docker nằm trong [`.docker/bridge/`](.docker/bridge/).

Build image:

```bash
.docker/bridge/build.sh
```

Khởi động bridge:

```bash
.docker/bridge/run.sh
```

Bridge sử dụng `--network=host` để kết nối đến database trên host (127.0.0.1) và để judge kết nối được qua port 9999.

Kiểm tra bridge đang chạy:

```bash
docker logs bridge
```

### Cài đặt Judge (Docker)

LQDOJ sử dụng judge chạy trên Docker từ [DMOJ judge-server](https://github.com/DMOJ/judge-server). Các script nằm trong [`.docker/judge/`](.docker/judge/).

#### Build Judge Image

Clone repo judge-server cạnh thư mục online-judge (nếu chưa có):

```bash
git clone https://github.com/LQDJudge/judge-server.git
```

Build Docker image:

```bash
.docker/judge/build_image.sh
```

Mặc định script tìm `judge-server/` cạnh `online-judge/`. Đặt `JUDGE_SERVER_DIR` để thay đổi.

> **Mẹo:** Image `tierlqdoj` đầy đủ chứa tất cả runtime và build rất lâu. Để test nhanh, có thể build `tier1` (Python 2/3, C/C++, Java 8, Pascal):
> ```bash
> cd judge-server/.docker && make judge-tier1
> ```
> Sau đó đặt `JUDGE_IMAGE=vnoj/judge-tier1:latest` khi chạy judge.

#### Đăng ký Judge

Đăng ký judge vào database (chạy từ thư mục `online-judge` với virtualenv đã activate):

```bash
.docker/judge/register_judges.sh 1 10 '<authentication key>'
```

Lệnh trên đăng ký `judge1` đến `judge10`. Bạn cũng có thể thêm qua admin: **Admin** -> **Judges** -> **Add Judge**.

#### Cấu hình Judge

Tạo file cấu hình trong thư mục problems (ví dụ: `/mnt/problems/__conf__/general.yml`). File mẫu đầy đủ tại [`sample_conf/judge.yml`](sample_conf/judge.yml). Tên judge được truyền qua command line, nên file cấu hình chỉ cần key và đường dẫn:

```yaml
key: '<authentication key>'
problem_storage_globs:
  - /problems/**/
runtime:
  g++: /usr/bin/g++
  gcc: /usr/bin/gcc
  python3: /usr/bin/python3
  # ... thêm runtime khác theo nhu cầu
```

Lưu ý: `problem_storage_globs` dùng `/problems/` vì đó là mount point bên trong Docker container.

#### Khởi động Judge

Đặt `PROBLEMS_DIR` trỏ đến thư mục problems trước khi chạy:

```bash
export PROBLEMS_DIR=/path/to/problems
```

Khởi động một judge:

```bash
.docker/judge/start_judge.sh judge1
```

Khởi động nhiều judge cùng lúc:

```bash
.docker/judge/start_judges.sh 1 5
```

Kiểm tra judge đã kết nối trong **Admin** -> **Judges** — judge sẽ hiển thị trạng thái online.

#### Judge trên server riêng

Đảm bảo port 9999 mở trên firewall của server site. Để chia sẻ dữ liệu bài tập giữa nhiều server, xem [Distributed Judges (JuiceFS)](#distributed-judges-juicefs).

### Tùy chọn: S3 Storage cho Media

Mặc định, media files được lưu trên filesystem local. Để dùng Amazon S3 hoặc storage tương thích S3 (ví dụ Cloudflare R2):

```bash
pip install django-storages[boto3]
```

Bỏ comment và cấu hình phần S3 trong `local_settings.py` (xem `sample_local_settings.py` để biết tất cả tùy chọn):

```python
AWS_ACCESS_KEY_ID = 'your-access-key'
AWS_SECRET_ACCESS_KEY = 'your-secret-key'
AWS_STORAGE_BUCKET_NAME = 'your-bucket-name'
AWS_S3_REGION_NAME = 'ap-southeast-1'
AWS_S3_CUSTOM_DOMAIN = 'cdn.example.com'
DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
```

Không cần thay đổi code — codebase sử dụng `default_storage` của Django nên mọi thao tác file tự động dùng S3 khi được cấu hình.

### Tùy chọn: Gợi ý bài tập bằng ML (Vector Search)

LQDOJ có hệ thống gợi ý bài tập sử dụng collaborative filtering và mô hình Two Tower với MariaDB vector search. Yêu cầu MariaDB 11.7+.

Xem [`judge/ml/README.md`](judge/ml/README.md) để biết hướng dẫn chi tiết.

### Crontab

Các cron job khuyến nghị cho production. Chỉnh sửa bằng `crontab -e`, thay `<venv>` và `<site>` bằng đường dẫn thực tế:

```crontab
0 4 * * * <venv>/bin/python3 <site>/manage.py cleanup_inactive --users --orgs
4 4 * * * <venv>/bin/python3 <site>/manage.py batch_clearsessions
7 4 * * * <venv>/bin/python3 <site>/manage.py recompute_comment_scores
10 4 * * * <venv>/bin/python3 <site>/manage.py delete_old_notifications
11 4 * * * <venv>/bin/python3 <site>/manage.py recompute_contributions
15 4 * * * <venv>/bin/python3 <site>/manage.py fix_organization_private
```

</details>