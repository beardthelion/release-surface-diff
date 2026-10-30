package main

import (
	pb "example.com/surface/gen/userpb"

	gethrpc "github.com/ethereum/go-ethereum/rpc"
	"google.golang.org/grpc"
)

type userSvc struct{}
type backend struct{}

func wireRPC(s *grpc.Server, b *backend) {
	s.RegisterService(&grpc.ServiceDesc{
		ServiceName: "user.User",
		HandlerType: nil,
	}, &userSvc{})
	pb.RegisterUserServer(s, &userSvc{})
	apis := []gethrpc.API{
		{Namespace: "eth"},
		{Namespace: "admin"},
	}
	_ = apis
}
