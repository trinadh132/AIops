# Public subnets only, no NAT gateway (~$32/month on its own). The mock
# service task gets a public IP for outbound calls (ECR, CloudWatch, SQS);
# inbound is locked to admin_cidr by its security group. The Lambdas run
# outside the VPC entirely.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.project }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = var.project }
}

# Two AZs so a Spot capacity shortage in one doesn't strand the service.
resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false # the ECS service asks for one explicitly
  tags                    = { Name = "${var.project}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.project}-public" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "mock_service" {
  name        = "${var.project}-mock-service"
  description = "Mock service: 8080 from the admin CIDR only"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "mock_admin" {
  security_group_id = aws_security_group.mock_service.id
  description       = "Failure injection and orders API from the admin"
  cidr_ipv4         = var.admin_cidr
  ip_protocol       = "tcp"
  from_port         = 8080
  to_port           = 8080
}

# HTTPS only. The destinations (ECR, S3 image layers, CloudWatch Logs, SQS)
# are AWS public endpoints with changing IPs, so the CIDR can't be narrowed
# without VPC interface endpoints (~$7/month each). DNS goes to the VPC
# resolver, which security groups don't filter.
#trivy:ignore:AWS-0104
resource "aws_vpc_security_group_egress_rule" "mock_https" {
  security_group_id = aws_security_group.mock_service.id
  description       = "HTTPS to AWS APIs: ECR, CloudWatch Logs, SQS"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
