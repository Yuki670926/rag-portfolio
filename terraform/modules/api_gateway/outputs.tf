output "api_endpoint" {
  value = "${aws_api_gateway_stage.main.invoke_url}/query"
}

output "rest_api_id" {
  value = aws_api_gateway_rest_api.main.id
}
