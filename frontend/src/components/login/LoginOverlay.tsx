import { useState, useRef, useEffect } from "react";
import { useAuth } from "@/lib/auth-context";

export function LoginOverlay() {
  const { isAuthenticated, isLoading, sendCode, login } = useAuth();
  const [step, setStep] = useState<"email" | "code">("email");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState(["", "", "", "", "", ""]);
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [resendSeconds, setResendSeconds] = useState(0);
  const inputRefs = useRef<(HTMLInputElement | null)[]>([]);

  useEffect(() => {
    if (resendSeconds <= 0) return;
    const t = setInterval(() => setResendSeconds((s) => s - 1), 1000);
    return () => clearInterval(t);
  }, [resendSeconds]);

  if (isLoading) return null;
  if (isAuthenticated) return null;

  const handleSendCode = async () => {
    setError("");
    if (!email.includes("@")) {
      setError("Please enter a valid email");
      return;
    }
    setSending(true);
    try {
      await sendCode(email);
      setStep("code");
      setResendSeconds(60);
      setTimeout(() => inputRefs.current[0]?.focus(), 100);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSending(false);
    }
  };

  const handleCodeInput = (idx: number, value: string) => {
    const digit = value.replace(/\D/g, "").slice(-1);
    const next = [...code];
    next[idx] = digit;
    setCode(next);
    if (digit && idx < 5) inputRefs.current[idx + 1]?.focus();
  };

  const handleCodeKeyDown = (idx: number, e: React.KeyboardEvent) => {
    if (e.key === "Backspace" && !code[idx] && idx > 0) {
      inputRefs.current[idx - 1]?.focus();
    }
    if (e.key === "Enter") handleVerify();
  };

  const handlePaste = (e: React.ClipboardEvent) => {
    e.preventDefault();
    const pasted = e.clipboardData.getData("text").replace(/\D/g, "").slice(0, 6);
    const next = [...code];
    pasted.split("").forEach((d, i) => (next[i] = d));
    setCode(next);
    if (pasted.length === 6) {
      setTimeout(() => handleVerifyWith(next.join("")), 50);
    }
  };

  const handleVerify = () => handleVerifyWith(code.join(""));

  const handleVerifyWith = async (codeStr: string) => {
    if (codeStr.length !== 6) {
      setError("Please enter the 6-digit code");
      return;
    }
    setVerifying(true);
    setError("");
    try {
      await login(email, codeStr);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setVerifying(false);
    }
  };

  const handleResend = async () => {
    if (resendSeconds > 0) return;
    setError("");
    try {
      await sendCode(email);
      setResendSeconds(60);
      setCode(["", "", "", "", "", ""]);
      inputRefs.current[0]?.focus();
    } catch (e: any) {
      setError(e.message);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="w-[380px] max-w-[90vw] rounded-xl border border-border bg-surface p-8 text-foreground shadow-lg">
        <h2 className="mb-1 text-center font-serif text-xl font-medium">MultiClaw</h2>
        <p className="mb-5 text-center text-xs uppercase tracking-wider text-muted-foreground">
          Sign in to continue
        </p>

        {step === "email" ? (
          <>
            <label className="mb-1 block text-sm text-muted-foreground">Email</label>
            <input
              type="email"
              className="mb-3 w-full rounded-lg border border-border bg-input px-3 py-2.5 text-sm outline-none focus:border-accent"
              placeholder="hello@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSendCode()}
              autoFocus
            />
            <button
              className="w-full rounded-lg bg-accent py-2.5 text-sm font-medium text-background hover:brightness-110 disabled:opacity-40"
              onClick={handleSendCode}
              disabled={sending}
            >
              {sending ? "Sending..." : "Send Code"}
            </button>
            {error && <p className="mt-3 text-center text-xs text-danger">{error}</p>}
          </>
        ) : (
          <>
            <p className="mb-3 text-sm text-muted-foreground">
              Sent to <strong>{email}</strong>
            </p>
            <div className="mb-4 flex justify-center gap-2" onPaste={handlePaste}>
              {code.map((d, i) => (
                <input
                  key={i}
                  ref={(el) => { inputRefs.current[i] = el; }}
                  className="h-[54px] w-[44px] rounded-lg border border-border bg-input text-center font-mono text-2xl outline-none focus:border-accent"
                  maxLength={1}
                  inputMode="numeric"
                  pattern="[0-9]"
                  value={d}
                  onChange={(e) => handleCodeInput(i, e.target.value)}
                  onKeyDown={(e) => handleCodeKeyDown(i, e)}
                />
              ))}
            </div>
            <button
              className="w-full rounded-lg bg-accent py-2.5 text-sm font-medium text-background hover:brightness-110 disabled:opacity-40"
              onClick={handleVerify}
              disabled={verifying}
            >
              {verifying ? "Verifying..." : "Verify"}
            </button>
            {error && <p className="mt-3 text-center text-xs text-danger">{error}</p>}
            <p className="mt-3 text-center text-xs text-muted-foreground">
              <span
                className={resendSeconds > 0 ? "cursor-not-allowed opacity-50" : "cursor-pointer text-accent hover:underline"}
                onClick={handleResend}
              >
                {resendSeconds > 0 ? `Resend code (${resendSeconds}s)` : "Resend code"}
              </span>
            </p>
            <p
              className="mt-2 cursor-pointer text-center text-xs text-muted-foreground underline hover:text-accent"
              onClick={() => { setStep("email"); setError(""); }}
            >
              &larr; Use a different email
            </p>
          </>
        )}
      </div>
    </div>
  );
}
