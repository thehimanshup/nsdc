# Windows reverse proxy (IIS + ARR)

uvicorn runs on `127.0.0.1:8000` (via NSSM — see `install-service.ps1`). IIS sits
in front as the TLS terminator + reverse proxy, the same role nginx plays on Linux.

## 1. Enable required IIS features
- **Web Server (IIS)** role
- **Application Request Routing (ARR) 3.0** (separate download)
- **URL Rewrite 2.1** (separate download)
- **WebSocket Protocol** — *mandatory*, or streaming replies break
  (`Add Roles and Features → Web Server → Application Development → WebSocket Protocol`)

## 2. Enable the proxy
IIS Manager → server node → **Application Request Routing Cache** → *Server Proxy
Settings* → tick **Enable proxy**.

## 3. Internal (ZTNA) site — serves the whole app
Create an HTTPS site bound to the internal hostname the ZTNA connector targets,
with an internal/self-signed cert. Add a `web.config` in the site root:

```xml
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <rule name="ReverseProxyToUvicorn" stopProcessing="true">
          <match url="(.*)" />
          <action type="Rewrite" url="http://127.0.0.1:8000/{R:1}" />
        </rule>
      </rules>
    </rewrite>
    <webSocket enabled="true" />
    <security>
      <requestFiltering>
        <requestLimits maxAllowedContentLength="26214400" /> <!-- 25 MB uploads -->
      </requestFiltering>
    </security>
  </system.webServer>
</configuration>
```

## 4. Public webhook site — Twilio only (only if live voice/WhatsApp is in scope)
A SEPARATE IIS site on the public interface, public DNS = `PUBLIC_BASE_URL`,
publicly trusted TLS cert. Rewrite ONLY the webhook + audio paths; block the rest:

```xml
<rules>
  <rule name="TwilioWebhooks" stopProcessing="true">
    <match url="^(webhooks/twilio/.*|api/v1/audio/.*)$" />
    <action type="Rewrite" url="http://127.0.0.1:8000/{R:0}" />
  </rule>
  <rule name="BlockEverythingElse" stopProcessing="true">
    <match url=".*" />
    <action type="CustomResponse" statusCode="404" />
  </rule>
</rules>
```

Keep `TWILIO_VALIDATE_SIGNATURES=true` — the app rejects unsigned/forged callbacks
regardless of the proxy.
