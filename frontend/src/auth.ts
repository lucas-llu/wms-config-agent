import Keycloak from "keycloak-js";
import type { Auth } from "./types";

export async function authenticate(): Promise<{
  auth: Auth;
  authenticated: boolean;
}> {
  const response = await fetch("/v1/web-config", {
    cache: "no-store",
    credentials: "omit",
  });
  if (!response.ok) throw new Error("身份服务尚未配置，请联系管理员。");
  const config: { issuer: string; client_id: string } = await response.json();
  const url = new URL(config.issuer);
  if (
    url.protocol !== "https:" &&
    !(
      url.protocol === "http:" &&
      ["127.0.0.1", "localhost"].includes(url.hostname)
    )
  )
    throw new Error("身份服务需要 HTTPS。");
  const [server, realm] = config.issuer.split("/realms/");
  if (!realm || !config.client_id) throw new Error("身份服务配置无效。");
  const keycloak = new Keycloak({
    url: server,
    realm,
    clientId: config.client_id,
  });
  const redirectUri = location.origin + "/";
  const authenticated = await keycloak.init({
    onLoad: "check-sso",
    pkceMethod: "S256",
    responseMode: "fragment",
    checkLoginIframe: false,
    redirectUri,
    scope: "openid email profile",
  });
  const auth: Auth = {
    token: async () => {
      await keycloak.updateToken(30);
      if (!keycloak.token) throw new Error("请重新登录。");
      return keycloak.token;
    },
    login: () => keycloak.login({ redirectUri }),
    register: () => keycloak.register({ redirectUri }),
    recover: () => keycloak.login({ redirectUri }),
    changePassword: () =>
      keycloak.login({ redirectUri, action: "UPDATE_PASSWORD", maxAge: 0 }),
    logout: () => keycloak.logout({ redirectUri }),
  };
  return { auth, authenticated };
}
