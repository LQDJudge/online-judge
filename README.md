# online-judge
### 1. Activate virtualenv:
source dmojsite/bin/activate
### 2. Remember to change the local_settings

### 3. Run server:
python manage.py runserver 0.0.0.0:8000

### 4. Create configure file for judge:
python dmojauto-conf

### 5. Create folder for problems, change the dir in judge conf file and local_settings.py

### 6. Connect judge:
+ python manage.py runbridged
+ dmoj 0.0.0.0 -p 9999 -c judge/conf1.yml (depend on port in the local_settings.py and directory of conf file)

### 7. Update vietnamese translation:
 - go to locale/vi
 - modify .po file
 - python manage.py compilemessages
 - python manage.py compilejsi18n

###8. Run chat server:
docker run -p 6379:6379 -d redis:2.8
