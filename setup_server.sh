#!/bin/bash
# ========================================================
# AutoStock - 오라클 클라우드 (Ubuntu) 원클릭 환경설정 스크립트
# ========================================================
set -e

echo "=== [1/5] 한국 시간대(KST, Asia/Seoul) 설정 ==="
sudo timedatectl set-timezone Asia/Seoul
echo "현재 서버 시간: $(date)"

echo "=== [2/5] 패키지 업데이트 및 Python 3.11/venv 설치 ==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git

echo "=== [3/5] 파이썬 가상환경(.venv) 생성 및 패키지 설치 ==="
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "=== [4/5] systemd 서비스 등록 (24시간 무중단 자동 재시작) ==="
sudo cp autostock.service /etc/systemd/system/autostock.service
sudo systemctl daemon-reload
sudo systemctl enable autostock.service

echo "=== [5/5] AutoStock 데몬 시작 ==="
sudo systemctl restart autostock.service

echo ""
echo "========================================================"
echo "🎉 AutoStock 24시간 백그라운드 서비스 등록이 완료되었습니다!"
echo "• 서비스 상태 확인: sudo systemctl status autostock"
echo "• 실시간 로그 확인: tail -f autostock.log"
echo "• 서비스 중단: sudo systemctl stop autostock"
echo "• 서비스 재시작: sudo systemctl restart autostock"
echo "========================================================"
