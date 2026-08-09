import { useState, useEffect, useCallback, type ReactNode } from "react";
import { authApi } from "./api";
import { AuthContext } from "./auth-context-store";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [email, setEmail] = useState<string | null>(null);
  const [userId, setUserId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    authApi
      .me()
      .then((data) => {
        setEmail(data.email ?? null);
        setUserId(data.user_id ?? null);
      })
      .catch(() => {})
      .finally(() => setIsLoading(false));
  }, []);

  const sendCode = useCallback(async (emailAddr: string) => {
    await authApi.sendCode(emailAddr);
  }, []);

  const login = useCallback(async (emailAddr: string, code: string) => {
    await authApi.verify(emailAddr, code);
    setEmail(emailAddr);
    const data = await authApi.me();
    setUserId(data.user_id ?? null);
  }, []);

  const logout = useCallback(async () => {
    await authApi.logout();
    setEmail(null);
    setUserId(null);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        email,
        userId,
        isLoading,
        isAuthenticated: email !== null,
        login,
        sendCode,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}
