LQDOJ: Le Quy Don Online Judge
===

[![](https://github.com/DMOJ/online-judge/workflows/build/badge.svg)](https://lqdoj.edu.vn/)
[![Python](https://img.shields.io/pypi/pyversions/tensorflow.svg?style=plastic)](https://python.org)
[![OS](https://img.shields.io/badge/Ubuntu-16.04%20%7C%2018.04%20%7C%2020.04-brightgreen)](https://ubuntu.com/download)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](https://www.gnu.org/licenses/agpl-3.0.en.html)

# Overview

Homepage: [https://lqdoj.edu.vn](https://lqdoj.edu.vn)

Based on [DMOJ](https://dmoj.ca/).

Supported languages:

- Assembly (x64)
- AWK
- C
- C++03 / C++11 / C++14 / C++17 / C++20
- Java 11
- Pascal
- Perl
- Python 2 / Python 3
- PyPy 2 / PyPy 3

Support plagiarism detection via [Stanford MOSS](https://theory.stanford.edu/~aiken/moss/).

# Installation

Most of the setup are the same as DMOJ installations. You can view the installation guide of DMOJ
here: https://docs.dmoj.ca/#/site/installation.
There is one minor change: Instead of `git clone https://github.com/DMOJ/site.git`, you clone this repo
`git clone https://github.com/LQDJudge/online-judge.git`.

- **Bước 1**: cài các thư viện cần thiết
    - $ ở đây nghĩa là sudo. Ví dụ dòng đầu nghĩa là chạy lệnh `sudo apt update`

```bash
$ apt update
$ apt install git gcc g++ make python3-dev python3-pip libxml2-dev libxslt1-dev zlib1g-dev gettext curl redis-server pkg-config
$ curl -sL https://deb.nodesource.com/setup_18.x | sudo -E bash -
$ apt install nodejs
$ npm install -g sass postcss-cli postcss autoprefixer
```

- **Bước 2**: tạo DB
    - Server đang dùng `MariaDB ≥ 10.5`, các bạn cũng có thể dùng `MySQL` nếu bị conflict
    - Nếu các bạn chạy lệnh dưới này xong mà version `MariaDB` bị cũ (< 10.5) thì có thể tra google cách cài `MariaDB`
      mới
      nhất (10.5 hoặc 10.6).
    - Các bạn có thể thấy version `MariaDB` bằng cách gõ lệnh `sudo mysql` (Ctrl + C để quit)

```bash
$ apt update
$ apt install mariadb-server libmysqlclient-dev
```

- **Bước 3**: tạo table trong DB
    - Các bạn có thể thay tên table và password

```bash
$ sudo mysql
mariadb> CREATE DATABASE dmoj DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_general_ci;
mariadb> GRANT ALL PRIVILEGES ON dmoj.* TO 'dmoj'@'localhost' IDENTIFIED BY '<password>';
mariadb> exit
```

- **Bước 4**: Cài đặt môi trường ảo (virtual env) và pull code
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

- **Bước 5**: Tạo `local_settings.py`. Đây là file để custom setting cho Django. Các bạn tạo file vào
  `online-judge/dmoj/local_settings.py`
    - File mẫu: `online-judge/dmoj/sample_local_settings.py`
    - Nếu bạn đổi tên hoặc mật khẩu table databases thì thay đổi thông tin tương ứng trong `Databases`
    - Sau khi xong, chạy lệnh `python3 manage.py check` để kiểm tra
- **Bước 6**: Compile CSS và translation
    - Giải thích: Lệnh 1 và 2 gọi sau mỗi lần thay đổi 1 file css hoặc file js (file html thì không cần)
        - Lệnh 3 và 4 gọi sau mỗi lần thay đổi file dịch
    - Note: Sau khi chạy lệnh này, folder tương ứng với `STATIC_ROOT` trong `local_settings.py` phải được tạo. Nếu chưa
      được
      tạo thì mình cần tạo folder đó trước khi chạy 2 lệnh đầu.

```bash
$ ./make_style.sh
$ bash manage.py collectstatic
$ python3 manage.py compilemessages
$ python3 manage.py compilejsi18n
```

- **Bước 7**: Thêm dữ liệu vào DB

```bash
$ python3 manage.py migrate
$ python3 manage.py loaddata navbar
$ python3 manage.py loaddata language_small
$ python3 manage.py loaddata demo
```

- **Bước 8**: Chạy site. Đến đây thì cơ bản đã hoàn thành (chưa có judge, websocket, celery). Các bạn có thể truy cập
  tại `localhost:8000`

```bash
python3 manage.py runserver 0.0.0.0:8000
```

**Một số lưu ý:**

1. (WSL) có thể tải ứng dụng Terminal trong Windows Store
2. (WSL) mỗi lần mở ubuntu, các bạn cần chạy lệnh sau để mariadb khởi động: `sudo service mysql restart` (tương tự cho
   một số service khác như memcached, celery)
3. Sau khi cài đặt, các bạn chỉ cần activate virtual env và chạy lệnh runserver là ok

    ```bash
    . dmojsite/bin/activate
    python3 manage.py runserver
    ```

4. Đối với nginx, sau khi config xong theo guide của DMOJ, bạn cần thêm location như sau để sử dụng được tính năng
   profile image, thay thế `path/to/oj` thành đường dẫn nơi bạn đã clone source code.

    ```
    location /profile_images/ {
        root /path/to/oj;
    }
    ```

5. Quy trình dev:
    1. Sau khi thay đổi code thì django tự build lại, các bạn chỉ cần F5
    2. Một số style nằm trong các file .scss. Các bạn cần recompile css thì mới thấy được thay đổi.

## Optional

### Alias

Các bạn có thể lưu các alias này để sau này dùng cho nhanh

- `mtrans`: để generate translation khi các bạn add một string trong code
- `trans`: compile translation (sau khi bạn đã dịch tiếng Việt)
- `cr`: chuyển tới folder OJ
- `pr`: chạy server
- `sm`: restart service (chủ yếu dùng cho WSL)
- `sd`: activate virtual env
- `css`: compile các file css

```bash
alias mtrans='python3 manage.py makemessages -l vi && python3 manage.py makedmojmessages -l vi'
alias pr='python3 manage.py runserver'
alias sd='source ~/LQDOJ/dmojsite/bin/activate'
alias sm='sudo service mysql restart && sudo service redis-server start && sudo service memcached start'
alias trans='python3 manage.py compilemessages -l vi && python3 manage.py compilejsi18n -l vi'
alias cr='cd ~/LQDOJ/online-judge'
alias css='./make_style.sh && python3 manage.py collectstatic --noinput'
```

### Memcached

Dùng cho in-memory cache

```bash
$ sudo apt install memcached
```

### Websocket

Dùng để live update (như chat)

- Tạo file `online-judge/websocket/config.js`

```jsx
module.exports = {
    get_host: '127.0.0.1',
    get_port: 15100,
    post_host: '127.0.0.1',
    post_port: 15101,
    http_host: '127.0.0.1',
    http_port: 15102,
    long_poll_timeout: 29000,
};
```

- Cài các thư viện

```bash
$ npm install qu ws simplesets
$ pip3 install websocket-client
```

- Khởi động (trong 1 tab riêng)

```bash
$ node websocket/daemon.js
```

### Celery

Dùng cho một số task như batch rejudge

```bash
celery -A dmoj_celery worker
```

### Judge

Cài đặt ở 1 folder riêng bên ngoài site:

```bash
$ apt install python3-dev python3-pip build-essential libseccomp-dev
$ git clone https://github.com/LQDJudge/judge-server.git
$ cd judge-server
$ sudo pip3 install -e .
```

- Tạo một file judge.yml ở bên ngoài folder judge-server (file
  mẫu https://github.com/DMOJ/docs/blob/master/sample_files/judge_conf.yml)
- Thêm judge vào site bằng UI: Admin → Judge → Thêm Judge → nhập id và key (chỉ cần thêm 1 lần) hoặc dùng lệnh
  `python3 managed.py addjudge <id> <key>`.
- Chạy Bridge (cầu nối giữa judge và site) trong 1 tab riêng trong folder online-judge:

```bash
$ python3 managed.py runbridged
```

- Khởi động Judge (trong 1 tab riêng):

```bash
$ dmoj -c judge.yml localhost
```

- Lưu ý: mỗi lần sau này muốn chạy judge thì mở 1 tab cho bridge và n tab cho judge. Mỗi judge cần 1 file yml khác
  nhau (chứa authentication khác nhau)

## Some frequent difficulties when installation:

1. Missing the `local_settings.py`. You need to copy the `local_settings.py` in order to pass the check.
2. Missing the problem folder in `local_settings.py`. You need to create a folder to contain all problem packages and
   configure in `local_settings.py`.
3. Missing static folder in `local_settings.py`. Similar to problem folder, make sure to configure `STATIC_FILES` inside
   `local_settings.py`.
4. Missing configure file for judges. Each judge must have a seperate configure file. To create this file, you can run
   `python dmojauto-conf`. Checkout all sample files here https://github.com/DMOJ/docs/blob/master/sample_files.
5. Missing timezone data for SQL. If you're using Ubuntu and you're following DMOJ's installation guide for the server,
   and you are getting the error mentioned in https://github.com/LQDJudge/online-judge/issues/45, then you can follow
   this method to fix:

    ```bash
    # You may have to do this if you haven't set root password for MySQL, replace mypass with your password
    # SET PASSWORD FOR 'root'@'localhost' = PASSWORD('mypass');
    # FLUSH PRIVILEGES;
    mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql -D mysql -u root -p 
    mysql -u root -p -e "flush tables;" mysql
    ```

6. Missing the chat secret key, you must generate a Fernet key, and assign a variable in `local_settings.py` like this

    ```python
    CHAT_SECRET_KEY = "81HqDtbqAywKSOumSxxxxxxxxxxxxxxxxx="
    ```

# Usage

Suppose you finished all the installation. Everytime you want to run a local server, follow these steps:

1. Activate virtualenv:

    ```bash
    source dmojsite/bin/activate
    ```

2. Run server:

    ```bash
    python3 manage.py runserver 0.0.0.0:8000
    ```

3. Create a bridge (this is opened in a different terminal with the second step if you are using the same machine)

    ```bash
    python3 manage.py runbridged
    ```

4. Create a judge (another terminal)

    ```bash
    dmoj 0.0.0.0 -p 9999 -c <path to yml configure file>
    ```

    Here we suppose you use the default port `9999` for bridge in `settings.py`. You can create multiple judges, each should
    be in a seperate terminal.

### Optional

1. Run celery worker (This is server's queue. It may be necessary in some functions)

    ```bash
    celery -A dmoj_celery worker
    ```

2. Run a live event server (So everything is updated lively like in the production)

    ```bash
    node websocket/daemon.js
    ```

3. To use subdomain for each organization, go to admin page -> navigation bar -> sites, add domain name (e.g,
   `localhost:8000`). Then go to add `USE_SUBDOMAIN = True` to `local_settings.py`.

# Deploy

Most of the steps are similar to Django tutorials. Here are two usual steps:

1. Update Vietnamese translation:

    - If you add any new phrases in the code, ```python3 manage.py makemessages```
    - go to `locale/vi`
    - modify `.po` file
    - `python3 manage.py compilemessages`
    - `python3 manage.py compilejsi18n`

2. Update styles (using SASS)

    - Change .css/.scss files in `resources` folder
    - `./make_style.sh && python3 manage.py collectstatic`
    - Sometimes you need to press `Ctrl + F5` to see the new user interface in browser.

# Screenshots

## Leaderboard

Leaderboard with information about contest rating, performance points and real name of all users.

![](https://raw.githubusercontent.com/emladevops/LQDOJ-image/main/brave_SK67WA26FA.png#gh-light-mode-only)
![](https://raw.githubusercontent.com/emladevops/LQDOJ-image/main/brave_cmqqCnwaFc.png#gh-dark-mode-only)

## Admin dashboard

Admin dashboard helps you easily managing problems, users, contests and blog posts.

![](https://i.imgur.com/iccr3mh.png)

## Statement editor

You can write the problems' statement in Markdown with LaTeX figures and formulas supported.

![](https://i.imgur.com/CQVC754.png)

## Chat

Users can communicate with each other and can see who's online.

![](https://raw.githubusercontent.com/emladevops/LQDOJ-image/main/brave_kPsC5bJluc.png#gh-light-mode-only)
![](https://raw.githubusercontent.com/emladevops/LQDOJ-image/main/brave_AtrEzXzEAx.png#gh-dark-mode-only)
