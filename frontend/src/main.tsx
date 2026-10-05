import { createRoot } from "react-dom/client";
import { authenticate } from "./auth";
import { Api } from "./api";
import { WorkbenchApp, Welcome } from "./Workbench";
import "./style.css";

const root = createRoot(document.getElementById("root")!);
authenticate()
  .then(({ auth, authenticated }) => {
    root.render(
      authenticated ? (
        <WorkbenchApp api={new Api(auth)} auth={auth} />
      ) : (
        <Welcome auth={auth} />
      ),
    );
  })
  .catch(() =>
    root.render(
      <main className="welcome">
        <div className="brandmark">W</div>
        <h1>暂时无法连接身份服务</h1>
        <p>请联系管理员检查登录配置。现有工作台不会被修改。</p>
        <button onClick={() => location.reload()}>重新连接</button>
      </main>,
    ),
  );
