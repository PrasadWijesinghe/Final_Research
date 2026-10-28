import type { ReactNode } from "react";
import { Shell } from "./components/Shell";
import { ErrorBox, Loading } from "./components/ui";
import { AuthProvider, useAuth } from "./lib/auth";
import { type Route, useRoute } from "./lib/router";
import CommitPage from "./pages/CommitPage";
import Dashboard from "./pages/Dashboard";
import FailureDetail from "./pages/FailureDetail";
import Failures from "./pages/Failures";
import History from "./pages/History";
import InfraDemo from "./pages/InfraDemo";
import Settings from "./pages/Settings";
import SignIn from "./pages/SignIn";

function Protected({ children }: { children: ReactNode }) {
  const { auth, refresh } = useAuth();
  if (auth.status === "loading") return <Loading what="Checking sign-in" />;
  if (auth.status === "error") return <ErrorBox message={auth.message} onRetry={refresh} />;
  if (auth.status === "anon") return <SignIn />;
  return <>{children}</>;
}

function Page({ route }: { route: Route }) {
  switch (route.name) {
    case "infra":
      return <InfraDemo />;
    case "signin":
      return <SignIn />;
    case "dashboard":
      return <Protected><Dashboard /></Protected>;
    case "history":
      return <Protected><History /></Protected>;
    case "commit":
      return <Protected><CommitPage key={route.sha} sha={route.sha} /></Protected>;
    case "failures":
      return <Protected><Failures /></Protected>;
    case "failure":
      return <Protected><FailureDetail key={route.id} id={route.id} /></Protected>;
    case "settings":
      return <Protected><Settings /></Protected>;
    default:
      return <ErrorBox message={`No page at ${route.path}.`} />;
  }
}

export default function App() {
  const route = useRoute();
  return (
    <AuthProvider>
      <Shell route={route}>
        <Page route={route} />
      </Shell>
    </AuthProvider>
  );
}
