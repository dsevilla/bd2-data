#! /bin/sh

set -ex

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends mysql-server mysql-client python3 python3-pip python3-venv

service mysql start
mysql -uroot -proot -e "ALTER USER 'root'@'localhost' IDENTIFIED WITH mysql_native_password BY 'root'; FLUSH PRIVILEGES;"
mysql -uroot -proot -e "CREATE USER 'root'@'%' IDENTIFIED WITH mysql_native_password BY 'root'; GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' WITH GRANT OPTION; FLUSH PRIVILEGES;"
mysql -uroot -proot -e "SHOW GRANTS FOR 'root'@'localhost';"
mysql -uroot -proot -e "SHOW GRANTS FOR 'root'@'%';"

python3 -m venv ~/.venv
. ~/.venv/bin/activate
pip3 install nbconvert ipython

ipython3 generate-mysql-db.ipynb

(echo "SET NAMES 'utf8mb4' COLLATE 'utf8mb4_bin';"; \
 echo "SET GLOBAL innodb_redo_log_capacity = 1000 * 1024 * 1024;"; \
 echo "SET autocommit = 0;"; \
 echo "SET SESSION foreign_key_checks = 0;"; \
 echo "SET SESSION unique_checks = 0;"; \
 echo "SET SESSION sql_log_bin = 0;"; \
 mysqldump -u root -proot --compression-algorithms=zstd \
    --default-character-set=utf8mb4 \
    --add-drop-database -e -F --single-transaction \
    --quick --skip-comments --compact --databases stackoverflow; \
 echo "" ; \
 echo "COMMIT;" ; \
 echo "SET SESSION sql_log_bin = 1;" ; \
 echo "SET SESSION unique_checks = 1;" ; \
 echo "SET SESSION foreign_key_checks = 1;" ; \
 echo "ANALYZE TABLE stackoverflow.Posts;" ; \
 echo "ANALYZE TABLE stackoverflow.Comments;" ; \
 echo "ANALYZE TABLE stackoverflow.Votes;" ; \
 echo "ANALYZE TABLE stackoverflow.Tags;" ; \
 echo "ANALYZE TABLE stackoverflow.Users;" ) \
   | gzip -9 >es.stackoverflow.sql.gz

service mysql stop

# Delete old .gz.XX files when we know the process has gone OK.
rm -f es.stackoverflow.sql.gz.[0-9][0-9]

split -d -b 100M es.stackoverflow.sql.gz es.stackoverflow.sql.gz.
echo es.stackoverflow.sql.gz.[0-9][0-9] | tr " " "\n" > es.stackoverflow.sql.gz.list.txt