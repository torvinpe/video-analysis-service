# Deployment

Install gunicorn (`pip install gunicorn`), and:

```bash
gunicorn -w 4 -b 127.0.0.1:4000 app:app
```

Example nginx config (`/etc/nginx/conf.d/proxy-flask.conf`):

```
server {
    listen 80;

    server_name server-name-here;

    auth_basic "Password";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass         http://127.0.0.1:4000/;
        proxy_redirect     off;

        proxy_set_header   Host                 $host;
        proxy_set_header   X-Real-IP            $remote_addr;
        proxy_set_header   X-Forwarded-For      $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto    $scheme;
    }
}
```

Password file can be created with `htpasswd` command (`yum install httpd-tools`):

```bash
htpasswd -c /etc/nginx/.htpasswd username
```
