
#!/bin/bash

set -e



echo "==> 检查 Node.js 环境..."

if ! command -v node &> /dev/null; then

    echo "错误：未检测到 Node.js，请先安装 Node.js 以构建前端。"

    exit 1

fi



echo "==> 初始化环境..."

if [ ! -f .env ]; then

    cp .env.example .env

    # 自动生成 ENCRYPTION_KEY

    ENC_KEY=$(python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())" 2>/dev/null || echo "")

    if [ -n "$ENC_KEY" ]; then

        sed -i "s/^ENCRYPTION_KEY=$/ENCRYPTION_KEY=$ENC_KEY/" .env

        echo "已自动生成 ENCRYPTION_KEY"

    else

        echo "警告：无法自动生成 ENCRYPTION_KEY，请手动填写"

    fi

fi

set -a
source .env
set +a



mkdir -p browser-profiles releases backups logs

mkdir -p dashboard/dist  # 确保前端构建输出目录存在



echo "==> 构建前端..."

cd frontend && npm install && npm run build && cd ..



echo "==> 启动 Docker 服务..."

docker compose up -d



echo "==> 等待数据库就绪..."

sleep 10



echo "==> 初始化表结构..."

docker compose exec -T mysql mysql -u"${MYSQL_USER:-user}" -p"${MYSQL_PASSWORD:-password}" "${MYSQL_DATABASE:-lottery}" < init.sql



echo "==> 系统启动完成！ Dashboard: http://localhost"
