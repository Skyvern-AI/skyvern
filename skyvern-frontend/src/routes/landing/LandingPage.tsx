import { Button } from "@/components/ui/button";
import { Link } from "react-router-dom";
import {
  RocketIcon,
  LightningBoltIcon,
  EyeOpenIcon,
  LockClosedIcon,
  CodeIcon,
  GlobeIcon,
} from "@radix-ui/react-icons";

function LandingPage() {
  return (
    <div className="min-h-screen bg-gradient-to-b from-slate-950 via-slate-900 to-slate-950">
      {/* Header */}
      <header className="container mx-auto flex items-center justify-between px-6 py-6">
        <div className="flex items-center gap-3">
          <img
            src="https://aet4p1ka2mfpbmiq.public.blob.vercel-storage.com/logo-jadongai"
            alt="JadongAI"
            className="h-10"
          />
        </div>
        <div className="flex items-center gap-4">
          <Button variant="ghost" asChild>
            <Link to="/login">로그인</Link>
          </Button>
          <Button asChild>
            <Link to="/login">무료로 시작하기</Link>
          </Button>
        </div>
      </header>

      {/* Hero Section */}
      <section className="container mx-auto px-6 py-20 text-center">
        <div className="mx-auto max-w-4xl">
          <h1 className="mb-6 bg-gradient-to-r from-blue-400 via-purple-400 to-pink-400 bg-clip-text text-5xl font-bold leading-tight text-transparent md:text-6xl">
            AI로 웹 자동화를
            <br />
            새롭게 정의하다
          </h1>
          <p className="mb-10 text-xl text-slate-400">
            JadongAI는 인공지능과 컴퓨터 비전을 활용하여
            <br />
            복잡한 웹 작업을 자동화하는 차세대 플랫폼입니다.
          </p>
          <div className="flex flex-col items-center justify-center gap-4 sm:flex-row">
            <Button size="lg" className="px-8 py-6 text-lg" asChild>
              <Link to="/login">
                <RocketIcon className="mr-2 h-5 w-5" />
                지금 시작하기
              </Link>
            </Button>
            <Button
              size="lg"
              variant="outline"
              className="px-8 py-6 text-lg"
              asChild
            >
              <a
                href="https://docs.skyvern.com"
                target="_blank"
                rel="noopener noreferrer"
              >
                문서 보기
              </a>
            </Button>
          </div>
        </div>
      </section>

      {/* Features Section */}
      <section className="container mx-auto px-6 py-20">
        <h2 className="mb-16 text-center text-3xl font-bold text-white">
          왜 JadongAI인가요?
        </h2>
        <div className="grid gap-8 md:grid-cols-2 lg:grid-cols-3">
          <FeatureCard
            icon={<EyeOpenIcon className="h-8 w-8" />}
            title="컴퓨터 비전 기반"
            description="스크린샷을 분석하여 웹사이트의 구조 변화에도 유연하게 대응합니다. DOM에 의존하지 않아 안정적입니다."
          />
          <FeatureCard
            icon={<LightningBoltIcon className="h-8 w-8" />}
            title="자연어 명령"
            description="복잡한 코드 없이 자연어로 원하는 작업을 설명하세요. AI가 최적의 실행 경로를 찾아냅니다."
          />
          <FeatureCard
            icon={<CodeIcon className="h-8 w-8" />}
            title="워크플로우 빌더"
            description="드래그 앤 드롭으로 복잡한 자동화 워크플로우를 구성하세요. 코딩 없이도 가능합니다."
          />
          <FeatureCard
            icon={<LockClosedIcon className="h-8 w-8" />}
            title="안전한 인증 관리"
            description="로그인 정보와 2FA 코드를 안전하게 저장하고 자동화 작업에 활용할 수 있습니다."
          />
          <FeatureCard
            icon={<GlobeIcon className="h-8 w-8" />}
            title="글로벌 프록시"
            description="전 세계 다양한 지역의 프록시를 지원하여 지역 제한 없이 작업을 수행합니다."
          />
          <FeatureCard
            icon={<RocketIcon className="h-8 w-8" />}
            title="강력한 API"
            description="RESTful API와 웹훅을 통해 기존 시스템과 손쉽게 통합할 수 있습니다."
          />
        </div>
      </section>

      {/* Use Cases Section */}
      <section className="bg-slate-900/50 py-20">
        <div className="container mx-auto px-6">
          <h2 className="mb-16 text-center text-3xl font-bold text-white">
            다양한 활용 사례
          </h2>
          <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-4">
            <UseCaseCard
              title="데이터 수집"
              description="웹사이트에서 필요한 정보를 자동으로 추출하고 구조화합니다."
            />
            <UseCaseCard
              title="양식 자동 입력"
              description="반복적인 양식 작성 작업을 AI가 대신 처리합니다."
            />
            <UseCaseCard
              title="가격 모니터링"
              description="경쟁사 가격을 실시간으로 추적하고 알림을 받습니다."
            />
            <UseCaseCard
              title="테스트 자동화"
              description="웹 애플리케이션의 E2E 테스트를 자동으로 수행합니다."
            />
          </div>
        </div>
      </section>

      {/* CTA Section */}
      <section className="container mx-auto px-6 py-20 text-center">
        <div className="mx-auto max-w-2xl rounded-2xl bg-gradient-to-r from-blue-600/20 via-purple-600/20 to-pink-600/20 p-12">
          <h2 className="mb-4 text-3xl font-bold text-white">
            지금 바로 시작하세요
          </h2>
          <p className="mb-8 text-lg text-slate-400">
            복잡한 웹 자동화를 AI와 함께 간단하게 해결하세요.
          </p>
          <Button size="lg" className="px-10 py-6 text-lg" asChild>
            <Link to="/login">무료로 시작하기</Link>
          </Button>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t border-slate-800 py-12">
        <div className="container mx-auto px-6">
          <div className="flex flex-col items-center justify-between gap-4 md:flex-row">
            <div className="flex items-center gap-2">
              <img
                src="https://aet4p1ka2mfpbmiq.public.blob.vercel-storage.com/favicon-jadongai"
                alt="JadongAI"
                className="h-6 w-6"
              />
              <span className="text-sm text-slate-400">
                © 2024 JadongAI. All rights reserved.
              </span>
            </div>
            <div className="flex gap-6 text-sm text-slate-400">
              <a
                href="https://docs.skyvern.com"
                target="_blank"
                rel="noopener noreferrer"
                className="hover:text-white"
              >
                문서
              </a>
              <a href="mailto:support@jadong.shop" className="hover:text-white">
                문의하기
              </a>
            </div>
          </div>
        </div>
      </footer>
    </div>
  );
}

function FeatureCard({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-6 transition-all hover:border-slate-700 hover:bg-slate-900">
      <div className="mb-4 inline-flex rounded-lg bg-gradient-to-r from-blue-500/20 to-purple-500/20 p-3 text-blue-400">
        {icon}
      </div>
      <h3 className="mb-2 text-xl font-semibold text-white">{title}</h3>
      <p className="text-slate-400">{description}</p>
    </div>
  );
}

function UseCaseCard({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/30 p-6">
      <h3 className="mb-2 text-lg font-semibold text-white">{title}</h3>
      <p className="text-sm text-slate-400">{description}</p>
    </div>
  );
}

export { LandingPage };
