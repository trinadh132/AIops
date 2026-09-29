resource "aws_cloudwatch_log_group" "mock_service" {
  name              = "/ecs/${var.project}-mock-service"
  retention_in_days = var.log_retention_days # unbounded retention is the classic surprise bill
}

resource "aws_ecs_cluster" "main" {
  name = var.project

  setting {
    name  = "containerInsights"
    value = "disabled" # extra custom metrics per task: real money at this budget
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE_SPOT", "FARGATE"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
  }
}

# ---- IAM ------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role: what ECS itself needs to start the task (pull the image,
# ship stdout to CloudWatch).
resource "aws_iam_role" "mock_execution" {
  name               = "${var.project}-mock-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "mock_execution" {
  role       = aws_iam_role.mock_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task role: what the application code can do. Exactly: take messages off
# the remediation queue. Nothing else.
resource "aws_iam_role" "mock_task" {
  name               = "${var.project}-mock-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "mock_task" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage"]
    resources = [aws_sqs_queue.remediation.arn]
  }
}

resource "aws_iam_role_policy" "mock_task" {
  role   = aws_iam_role.mock_task.id
  policy = data.aws_iam_policy_document.mock_task.json
}

# ---- task + service ---------------------------------------------------------

resource "aws_ecs_task_definition" "mock_service" {
  family                   = "${var.project}-mock-service"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  # 1 GB: the JVM heap is 75% of this (MaxRAMPercentage in the Dockerfile).
  # 512 MB works but leaves little headroom for Spring + the AWS SDK.
  cpu                = 256
  memory             = 1024
  execution_role_arn = aws_iam_role.mock_execution.arn
  task_role_arn      = aws_iam_role.mock_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "mock-service"
    image     = local.mock_image
    essential = true

    portMappings = [{ containerPort = 8080, protocol = "tcp" }]

    environment = [
      { name = "REMEDIATION_QUEUE_URL", value = aws_sqs_queue.remediation.url },
      { name = "AWS_REGION", value = var.region },
    ]

    healthCheck = {
      command     = ["CMD-SHELL", "curl -fsS http://localhost:8080/actuator/health || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }

    # Spot gives a 2-minute warning; SIGTERM -> Spring stops the SQS poller
    # (SmartLifecycle) well inside this window.
    stopTimeout = 30

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.mock_service.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "mock"
      }
    }
  }])
}

resource "aws_ecs_service" "mock_service" {
  name            = "mock-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.mock_service.arn
  desired_count   = 1

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
  }

  # One task, no load balancer: stop the old task before starting the new
  # one, so a deploy never runs two pollers or pays for two tasks.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.mock_service.id]
    assign_public_ip = true # outbound to AWS APIs without a NAT gateway
  }

  depends_on = [aws_ecs_cluster_capacity_providers.main]
}
