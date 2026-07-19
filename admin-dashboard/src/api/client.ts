import { useAuthStore } from "@/store/auth";

const BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

class ApiError extends Error {
  status: number;
  constructor(
    status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function buildHeaders(isMutation = false): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };

  const { secret, operatorId } = useAuthStore.getState();
  if (secret) {
    headers["Authorization"] = `Bearer ${secret}`;
  }

  if (isMutation && operatorId) {
    headers["X-Operator-Id"] = operatorId;
  }

  return headers;
}

function buildQueryString(
  params?: Record<string, string | number | boolean | string[] | undefined>,
): string {
  if (!params) return "";

  const searchParams = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) continue;
    if (Array.isArray(value)) {
      for (const v of value) {
        searchParams.append(key, v);
      }
    } else {
      searchParams.set(key, String(value));
    }
  }

  const qs = searchParams.toString();
  return qs ? `?${qs}` : "";
}

async function handleResponse<T>(response: Response): Promise<T> {
  if (response.status === 401) {
    useAuthStore.getState().logout();
    throw new ApiError(401, "Unauthorized – session expired");
  }

  if (!response.ok) {
    const body = await response.text();
    throw new ApiError(
      response.status,
      `API error ${response.status}: ${body}`,
    );
  }

  return (await response.json()) as T;
}

export async function apiGet<T>(
  path: string,
  params?: Record<string, string | number | boolean | string[] | undefined>,
): Promise<T> {
  const url = `${BASE_URL}${path}${buildQueryString(params)}`;
  const response = await fetch(url, {
    method: "GET",
    headers: buildHeaders(),
  });
  return handleResponse<T>(response);
}

export async function apiPost<T>(
  path: string,
  body?: unknown,
): Promise<T> {
  const url = `${BASE_URL}${path}`;
  const headers = buildHeaders(true);
  headers["Idempotency-Key"] = crypto.randomUUID();

  const response = await fetch(url, {
    method: "POST",
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  return handleResponse<T>(response);
}
