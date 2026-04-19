#!/bin/bash
# Load timezone data into mysql DB (README Step 3: mariadb-tzinfo-to-sql)
# This runs inside the MariaDB container on first start.
mariadb-tzinfo-to-sql /usr/share/zoneinfo | mariadb -u root -p"$MYSQL_ROOT_PASSWORD" mysql
