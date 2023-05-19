LQDOJ: Le Quy Don Online Judge
===

[![](https://github.com/DMOJ/online-judge/workflows/build/badge.svg)](https://lqdoj.edu.vn/)
[![Python](https://img.shields.io/pypi/pyversions/tensorflow.svg?style=plastic)](https://python.org)
[![OS](https://img.shields.io/badge/Ubuntu-16.04%20%7C%2018.04%20%7C%2020.04-brightgreen)](https://ubuntu.com/download)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](https://www.gnu.org/licenses/agpl-3.0.en.html)

## Overview

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

## Installation

Most of the setup are the same as DMOJ installations. You can view the installation guide of DMOJ here: https://docs.dmoj.ca/#/site/installation.
There is one minor change: Instead of `git clone https://github.com/DMOJ/site.git`, you clone this repo `git clone https://github.com/LQDJudge/online-judge.git`.

### Some frequent difficulties when installation:

1. Missing the `local_settings.py`. You need to copy the `local_settings.py` in order to pass the check.
2. Missing the problem folder in `local_settings.py`. You need to create a folder to contain all problem packages and configure in `local_settings.py`.
3. Missing static folder in `local_settings.py`. Similar to problem folder, make sure to configure `STATIC_FILES` inside `local_settings.py`. 
4. Missing configure file for judges. Each judge must have a seperate configure file. To create this file, you can run `python dmojauto-conf`. Checkout all sample files here https://github.com/DMOJ/docs/blob/master/sample_files.
5. Missing timezone data for SQL. If you're using Ubuntu and you're following DMOJ's installation guide for the server, and you are getting the error mentioned in https://github.com/LQDJudge/online-judge/issues/45, then you can follow this method to fix:
```
mysql
-- You may have to do this if you haven't set root password for MySQL, replace mypass with your password
-- SET PASSWORD FOR 'root'@'localhost' = PASSWORD('mypass');
-- FLUSH PRIVILEGES;
exit
mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql -D mysql -u root -p 
mysql -u root -p -e "flush tables;" mysql
```
6. Missing the chat secret key, you must generate a Fernet key, and assign a variable in `local_settings.py` like this
```python
CHAT_SECRET_KEY = "81HqDtbqAywKSOumSxxxxxxxxxxxxxxxxx="
```


## Usage

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
Here we suppose you use the default port 9999 for bridge in `settings.py`. You can create multiple judges, each should be in a seperate terminal.

**Optional**

5. Run celery worker (This is server's queue. It may be necessary in some functions)
```bash
celery -A dmoj_celery worker
```

6. Run a live event server (So everything is updated lively like in the production)
```bash
node websocket/daemon.js
```

7. To use subdomain for each organization, go to admin page -> navigation bar -> sites, add domain name (e.g, "localhost:8000"). Then go to add `USE_SUBDOMAIN = True` to local_settings.py.

## Deploy
Most of the steps are similar to Django tutorials. Here are two usual steps:

1. Update vietnamese translation:
 - If you add any new phrases in the code, ```python3 manage.py makemessages```
 - go to `locale/vi`
 - modify `.po` file
 - ```python3 manage.py compilemessages```
 - ```python3 manage.py compilejsi18n```

2. Update styles (using SASS)
 - Change .css/.scss files in `resources` folder
 - ```./make_style.sh && python3 manage.py collectstatic```
 - Sometimes you need to press `Ctrl + F5` to see the new user interface in browser.
 
## Screenshots

### Leaderboard

Leaderboard with information about contest rating, performance points and real name of all users.

![](https://raw.githubusercontent.com/emladevops/LQDOJ-image/main/brave_SK67WA26FA.png#gh-light-mode-only)
![](https://raw.githubusercontent.com/emladevops/LQDOJ-image/main/brave_cmqqCnwaFc.png#gh-dark-mode-only)

### Admin dashboard

Admin dashboard helps you easily managing problems, users, contests and blog posts.

![](https://i.imgur.com/iccr3mh.png)

### Statement editor

You can write the problems' statement in Markdown with LaTeX figures and formulas supported.

![](https://i.imgur.com/CQVC754.png)

### Chat

Users can communicate with each other and can see who's online.

![](https://raw.githubusercontent.com/emladevops/LQDOJ-image/main/brave_kPsC5bJluc.png#gh-light-mode-only)
![](https://raw.githubusercontent.com/emladevops/LQDOJ-image/main/brave_AtrEzXzEAx.png#gh-dark-mode-only)
