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
- C++03 / C++11 / C++14 / C++17 / C++20
- Java 11
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
alias mtrans='python3 manage.py makemessages -l vi && python3 manage.py makedmojmessages -l vi'
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

- Create file `online-judge/websocket/config.js`

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

- Install libraries

```bash
$ npm install qu ws simplesets
$ pip3 install websocket-client
```

- Start (in a separate tab)

```bash
$ node websocket/daemon.js
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

## Common Installation Issues

1. **Missing `local_settings.py`**: You need to copy the `local_settings.py` in order to pass the check.
2. **Missing problem folder in `local_settings.py`**: You need to create a folder to contain all problem packages and configure in `local_settings.py`.
3. **Missing static folder in `local_settings.py`**: Similar to problem folder, make sure to configure `STATIC_FILES` inside `local_settings.py`.
4. **Missing configure file for judges**: Each judge must have a separate configure file. To create this file, you can run `python dmojauto-conf`. Check out all sample files here: https://github.com/DMOJ/docs/blob/master/sample_files.
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

# Screenshots

## Leaderboard

Leaderboard with information about contest rating, performance points and real name of all users.

![](https://raw.githubusercontent.com/emladevops/LQDOJ-image/main/brave_SK67WA26FA.png#gh-light-mode-only)
![](https://raw.githubusercontent.com/emladevops/LQDOJ-image/main/brave_cmqqCnwaFc.png#gh-dark-mode-only)

## Admin Dashboard

Admin dashboard helps you easily manage problems, users, contests and blog posts.

![](https://i.imgur.com/iccr3mh.png)

## Statement Editor

You can write problems' statements in Markdown with LaTeX figures and formulas supported.

![](https://i.imgur.com/CQVC754.png)

## Chat

Users can communicate with each other and can see who's online.

![](https://raw.githubusercontent.com/emladevops/LQDOJ-image/main/brave_kPsC5bJluc.png#gh-light-mode-only)
![](https://raw.githubusercontent.com/emladevops/LQDOJ-image/main/brave_AtrEzXzEAx.png#gh-dark-mode-only)

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
- C++03 / C++11 / C++14 / C++17 / C++20
- Java 11
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

## Các thành phần tùy chọn

### Alias hữu ích

Các bạn có thể lưu các alias này để sau này dùng cho nhanh:

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

Dùng cho in-memory cache:

```bash
$ sudo apt install memcached
```

### WebSocket

Dùng để live update (như chat):

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

</details>