LQDOJ: Le Quy Don Online Judge
===

[![](https://github.com/DMOJ/online-judge/workflows/build/badge.svg)](https://lqdoj.edu.vn/)
[![Python](https://img.shields.io/pypi/pyversions/tensorflow.svg?style=plastic)](https://python.org)
[![OS](https://img.shields.io/badge/Ubuntu-16.04%20%7C%2018.04%20%7C%2020.04-brightgreen)](https://ubuntu.com/download)

## Overview

Homepage: [https://lqdoj.edu.vn](https://lqdoj.edu.vn)

Supported languages: 

- Assembly (x64)
- AWK
- C
- C++03 / C++11 / C++14 / C++17
- Java 11
- Pascal
- Perl
- Python 2 / Python 3

## Installation

Activate virtualenv:
```bash
source dmojsite/bin/activate
```

Remember to update the `local_settings.py`.

Run server:
```bash
python manage.py runserver 0.0.0.0:8000
```

Create configure file for judge:
```bash
python dmojauto-conf
```

Create folder for problems, change the dir in judge conf file and `local_settings.py`.

Connect judge:
```bash
python manage.py runbridged
dmoj 0.0.0.0 -p 9999 -c judge/conf1.yml (depend on port in the local_settings.py and directory of conf file)
```

Update vietnamese translation:
 - go to `locale/vi`
 - modify `.po` file
 - ```bash python manage.py compilemessages```
 - ```bash python manage.py compilejsi18n```

Run chat server:
```bash
docker run -p 6379:6379 -d redis:2.8
```

## Screenshots

### Leaderboard

Leaderboard with information about contest rating, performance points and real name of all users.

![](https://i.imgur.com/ampxHXM.png)

### Admin dashboard

Admin dashboard helps you easily managing problems, users, contests and blog posts.

![](https://i.imgur.com/iccr3mh.png)

### Statement editor

You can write the problems' statement in Markdown with LaTeX figures and formulas supported.

![](https://i.imgur.com/CQVC754.png)

### Chat

Users can communicate with each other and can see who's online.

![](https://i.imgur.com/y9SGCgl.png)