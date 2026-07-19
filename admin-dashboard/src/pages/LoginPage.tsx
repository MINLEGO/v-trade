import { useState } from "react";
import { useNavigate, Navigate } from "react-router-dom";
import { useAuthStore } from "@/store/auth";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

const OPERATOR_ID_REGEX = /^[A-Za-z0-9_.:@-]{1,128}$/;

export default function LoginPage() {
  const { login, isAuthenticated } = useAuthStore();
  const navigate = useNavigate();
  const [secret, setSecret] = useState("");
  const [operatorId, setOperatorId] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (isAuthenticated) return <Navigate to="/" replace />;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (!secret.trim()) {
      setError("Admin secret is required.");
      return;
    }

    if (!operatorId.trim()) {
      setError("Operator ID is required.");
      return;
    }

    if (!OPERATOR_ID_REGEX.test(operatorId)) {
      setError(
        "Operator ID must be 1-128 characters: letters, digits, _ . : @ -",
      );
      return;
    }

    login(secret.trim(), operatorId.trim());
    navigate("/");
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-[#10141b]">
      <Card className="w-full max-w-sm border-[#1e2a38] bg-[#18202b]">
        <CardHeader>
          <CardTitle className="text-center text-xl font-bold text-[#8fd3ff]">
            V-Trade Admin
          </CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-1">
              <Input
                name="secret"
                type="password"
                placeholder="Admin Secret"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                className="bg-[#10141b]"
              />
            </div>

            <div className="space-y-1">
              <Input
                name="operatorId"
                type="text"
                placeholder="Operator ID"
                value={operatorId}
                onChange={(e) => setOperatorId(e.target.value)}
                className="bg-[#10141b]"
              />
            </div>

            {error && (
              <p className="text-sm text-red-400">{error}</p>
            )}

            <Button
              type="submit"
              className="w-full bg-[#8fd3ff] text-[#10141b] hover:bg-[#8fd3ff]/90"
            >
              Login
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}