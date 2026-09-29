# syntax=docker/dockerfile:1
# Build from the repo root:  docker build -f docker/mock-service.Dockerfile .

# ---- build: full JDK + Maven, discarded after packaging -------------------
FROM maven:3-eclipse-temurin-26 AS build
WORKDIR /build

# Dependencies first, sources second: editing Java code doesn't invalidate
# the (slow) dependency download layer.
COPY pom.xml .
RUN --mount=type=cache,target=/root/.m2 mvn -B -q dependency:go-offline

COPY src ./src
# Tests run in CI (mvn verify), not on every image build.
RUN --mount=type=cache,target=/root/.m2 mvn -B -q package -DskipTests

# ---- runtime: JRE only ----------------------------------------------------
FROM eclipse-temurin:17-jre

RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=build /build/target/self-healing-ops-mock-service-*.jar app.jar

# stdout-only logging (see logback-spring.xml); size the heap from the
# container's memory limit rather than the host's.
ENV SPRING_PROFILES_ACTIVE=container \
    JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=75"

USER app
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
