output "ecr_repositories" {
  value = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "approval_url" {
  value = module.approval_lambda.url
}

output "mcp_url" {
  description = "Append /mcp. Token: aws ssm get-parameter --with-decryption --name /<project>/MCP_AUTH_TOKEN"
  value       = var.enable_mcp ? module.mcp_lambda[0].url : null
}

output "runs_table" {
  value = aws_dynamodb_table.runs.name
}

output "remediation_queue_url" {
  value = aws_sqs_queue.remediation.url
}

output "mock_service_log_group" {
  value = aws_cloudwatch_log_group.mock_service.name
}

output "find_mock_service_ip" {
  description = "The Spot task's public IP changes on every replacement; this looks up the current one."
  value = join(" ", [
    "aws ecs list-tasks --cluster ${aws_ecs_cluster.main.name} --service-name ${aws_ecs_service.mock_service.name}",
    "--query 'taskArns[0]' --output text | xargs -I{} aws ecs describe-tasks --cluster ${aws_ecs_cluster.main.name} --tasks {}",
    "--query \"tasks[0].attachments[0].details[?name=='networkInterfaceId'].value\" --output text",
    "| xargs -I{} aws ec2 describe-network-interfaces --network-interface-ids {}",
    "--query 'NetworkInterfaces[0].Association.PublicIp' --output text",
  ])
}

output "set_external_secrets" {
  description = "Terraform only creates placeholders for these; set the real values once."
  value = [
    "aws ssm put-parameter --overwrite --type SecureString --name /${var.project}/OPENROUTER_API --value '<key>'",
    "aws ssm put-parameter --overwrite --type SecureString --name /${var.project}/DATABASE_URL --value 'postgresql://...?sslmode=require'",
  ]
}
