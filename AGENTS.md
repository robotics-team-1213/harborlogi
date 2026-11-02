# HarborLogi 에이전트 안내

절차의 정본은 [CONTRIBUTING.md](CONTRIBUTING.md)다. 이 파일은 에이전트가 자주 틀리는 저장소 규칙만 짚는다.

## 저장소 방향

정본은 `robotics-team-1213/harborlogi`(`origin`)의 `main`이다. `kernel-robot-8th/kernel-robot-repo-2`(`upstream`)는 팀 `main`이 `mirror-to-course` workflow로 자동 반영되는 사본이다. GitHub의 fork 표시와 `upstream`이라는 remote 이름은 반대로 보이지만 정본을 정하지 않는다. 자세한 내용은 [저장소 관계](CONTRIBUTING.md#저장소-관계-2026-11-01부터).

- 브랜치는 `origin/main`에서 만들고 `origin`에 push한다.
- PR은 `gh pr create -R robotics-team-1213/harborlogi --base main`으로 연다. `-R`을 빼면 gh가 fork parent인 과정 저장소에 연다.
- `upstream`에 push·PR·직접 커밋을 하지 않는다. `gh repo sync`와 웹의 "Sync fork"를 쓰지 않는다.
- 과정 저장소에만 커밋이 생겨 반영이 실패하면 덮어쓰지 말고, 그 커밋을 팀 저장소 PR로 가져온 뒤 `mirror-to-course`를 다시 돌린다.

## 병렬 세션

- 팀 `main`은 PR과 필수 검사로만 바뀐다([CONTRIBUTING §3](CONTRIBUTING.md#3-harbor를-고치는-흐름) 5번). 로컬에서 병합한 `main`을 push하지 않는다.
- 작업 공간은 `.worktrees/<주제>`에 한 주제당 하나 만든다. `main`을 체크아웃한 워크트리를 두지 않는다. 병합된 워크트리는 바로 지운다.
- `harbor_web/services.py`·`harbor_web/commands.py`는 여러 기능이 모이는 조립 파일이다. 동시에 한 세션만 고치고, 다른 세션은 그 PR이 병합된 뒤 새 `origin/main`에서 시작한다.
