output "api_endpoint" {
  value = "${aws_api_gateway_stage.main.invoke_url}/query"
}

output "rest_api_id" {
  value = aws_api_gateway_rest_api.main.id
}

output "root_resource_id" {
  value = aws_api_gateway_rest_api.main.root_resource_id
}

output "authorizer_id" {
  value = aws_api_gateway_authorizer.cognito.id
}

output "execution_arn" {
  value = aws_api_gateway_rest_api.main.execution_arn
}
