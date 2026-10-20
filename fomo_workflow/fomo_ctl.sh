#!/bin/bash
# ============================================================
# fomo_ctl.sh — FOMO 工作流进程管理脚本
# 用法: ./fomo_ctl.sh [start|stop|restart|status|log|tail]
# ============================================================

WORK_DIR="/root/.openclaw/workspace/fomo_workflow"
CONF="$WORK_DIR/supervisord.conf"
CTL="supervisorctl -c $CONF"

_check_supervisord() {
    # 沙箱重建后 pip 包会丢失：按需自恢复
    if ! command -v supervisord >/dev/null 2>&1; then
        echo "⚙️  supervisord 缺失，自动安装..."
        pip install --break-system-packages supervisor python-socks pysocks >/dev/null 2>&1
    fi
    if ! $CTL status > /dev/null 2>&1; then
        echo "⚙️  启动 supervisord..."
        supervisord -c "$CONF"
        sleep 3
    fi
}

case "$1" in
    start)
        _check_supervisord
        $CTL start fomo_workflow
        echo "✅ 已启动"
        $CTL status
        ;;
    stop)
        _check_supervisord
        $CTL stop fomo_workflow
        echo "🛑 已停止"
        ;;
    restart)
        _check_supervisord
        $CTL restart fomo_workflow
        sleep 2
        echo "🔄 已重启"
        $CTL status
        ;;
    status)
        _check_supervisord
        $CTL status
        ;;
    log)
        tail -${2:-100} "$WORK_DIR/trade.log"
        ;;
    tail)
        tail -f "$WORK_DIR/trade.log"
        ;;
    rotate)
        # 手动 log rotate
        STAMP=$(date +%Y%m%d_%H%M%S)
        mv "$WORK_DIR/trade.log" "$WORK_DIR/trade_${STAMP}.log"
        $CTL signal SIGUSR2 fomo_workflow 2>/dev/null || true
        echo "📦 日志已归档: trade_${STAMP}.log"
        ;;
    *)
        echo "用法: $0 [start|stop|restart|status|log [N]|tail|rotate]"
        exit 1
        ;;
esac
