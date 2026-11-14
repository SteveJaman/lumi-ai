import { useEffect, useRef, useState, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";

// Note the /hitl path: that is where modules/hitl_server.py listens.
const WS_URL = "ws://127.0.0.1:5090/hitl";
const MAX_BACKOFF = 10000;

export default function Island() {
  const [queue, setQueue] = useState([]); // pending approval requests
  const wsRef = useRef(null);
  const current = queue[0];

  useEffect(() => {
    let retry = 500;
    let timer;
    let closed = false;

    const connect = () => {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;
      ws.onopen = () => { retry = 500; };
      ws.onmessage = (e) => {
        let msg;
        try { msg = JSON.parse(e.data); } catch { return; }
        if (msg.type === "approval_request") {
          // Server re-sends open requests on reconnect; ignore duplicates.
          setQueue((q) => (q.some((r) => r.id === msg.id) ? q : [...q, msg]));
        } else if (msg.type === "approval_expired") {
          setQueue((q) => q.filter((r) => r.id !== msg.id));
        }
      };
      // Reconnect only after a close: a single timer, no polling.
      ws.onclose = () => {
        if (closed) return;
        setQueue([]); // the server has already handed these to the terminal
        timer = setTimeout(connect, retry);
        retry = Math.min(retry * 2, MAX_BACKOFF);
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      wsRef.current?.close();
    };
  }, []);

  // Under launch_island.py the window is click-through except while an
  // approval card is showing. No-op in a normal browser.
  useEffect(() => {
    window.pywebview?.api?.set_interactive?.(!!current);
  }, [current]);

  const respond = useCallback((id, decision) => {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "approval_response", id, decision }));
    }
    setQueue((q) => q.filter((r) => r.id !== id));
  }, []);

  return (
    <div className="fixed top-2 inset-x-0 z-50 flex justify-center pointer-events-none">
      <motion.div
        layout
        transition={{ type: "spring", stiffness: 380, damping: 32 }}
        style={{ borderRadius: current ? 28 : 999 }}
        className="pointer-events-auto bg-black text-white shadow-lg overflow-hidden"
        role={current ? "alertdialog" : undefined}
        aria-label={current ? `Approve ${current.tool}?` : undefined}
      >
        <AnimatePresence mode="wait" initial={false}>
          {current ? (
            <motion.div
              key={current.id}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="w-[min(92vw,380px)] p-4"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-semibold truncate">{current.tool}</span>
                {queue.length > 1 && (
                  <span className="text-xs text-white/50">+{queue.length - 1} more</span>
                )}
              </div>
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words text-xs text-white/70 font-mono">
                {current.preview}
              </pre>
              <div className="mt-3 flex gap-2">
                <button
                  onClick={() => respond(current.id, "decline")}
                  className="flex-1 rounded-full bg-white/10 py-2 text-sm hover:bg-white/20 transition-colors"
                >
                  Decline
                </button>
                <button
                  onClick={() => respond(current.id, "approve")}
                  className="flex-1 rounded-full bg-emerald-500 py-2 text-sm font-medium text-black hover:bg-emerald-400 transition-colors"
                >
                  Approve
                </button>
              </div>
            </motion.div>
          ) : (
            <motion.div key="pill" className="h-3 w-24" />
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  );
}
