# ========================================================
# AutoStock - 오라클 클라우드 원클릭 원격 업데이트 스크립트
# ========================================================
$KEY_PATH = "C:\Users\chqlw\Downloads\ssh-key-2026-10-28.key"
$REMOTE_HOST = "opc@161.33.13.112"
$REMOTE_DIR = "/home/opc/AutoStock/"

Write-Host "🚀 오라클 클라우드로 최신 코드를 전송합니다..." -ForegroundColor Cyan
scp -i "$KEY_PATH" -o StrictHostKeyChecking=no *.py requirements.txt tracked_targets.json buster_call_config.json "${REMOTE_HOST}:${REMOTE_DIR}"

if ($LASTEXITCODE -eq 0) {
    Write-Host "🔄 원격 서버 데몬을 재시작합니다..." -ForegroundColor Cyan
    ssh -i "$KEY_PATH" -o StrictHostKeyChecking=no "$REMOTE_HOST" "sudo systemctl restart autostock.service; sleep 2; sudo systemctl status autostock.service --no-pager"
    Write-Host "✅ 오라클 클라우드 업데이트 완료!" -ForegroundColor Green
} else {
    Write-Host "❌ 파일 전송 실패" -ForegroundColor Red
}
