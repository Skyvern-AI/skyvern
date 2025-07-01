# Skyvern Kubernetes Deployment

## REMINDER: It is not recommended to deploy Skyvern on the Internet without using some form of authentication! It is recommended to use this deployment for network without exposure to the Internet. 

## General 
This README has the purpose to explain the way Skyvern is deployed using Kubernetes.

One should take into consideration how it wants to deploy the application, either by using a service type LoadBalancer, which directly exposes port 8000 and 8080 on the host IP or by using ClusterIP service type, which requires an ingress and consequently a domain name.

This latter results in having the following endpoints:
> FRONTEND: http(s)://skyvern.example.com/

> BACKEND: http(s)://skyvern.example.com/api/

There is also a simple deploy script called `k8s-deploy.sh`, which runs the necessary commands to create the namespace and apply the `yaml` files.

If you look to redeploy from zero, make sure to delete the folders created on the hosts:

```
rm -rf /app/ /data/
```

## Environment variables

Environment variables must be set before running. So, before this type of deployment, the user is recommended to do the initial Skyvern setup to generate the backend's `.env` file, then copy the values from it to `backend-secrets.yaml` and add or either removed unused variables. You also have to replace the values in the `frontend-secrets.yaml` where needed

For the SKYVERN_API_KEY, run initially without setting any value, then copy the correct value from the `application frontend > settings > copy API key` and add it to the secrets files. Then, run `./k8s-deploy` again. If changes don't apply, delete the pods using:

```
kubectl delete pod -n skyvern -l app=skyvern-frontend
kubectl delete pod -n skyvern -l app=skyvern-backend
```

## TLS

If you decide to use TLS, uncomment the lines from the `ingress.yaml` related to it and replace with your own values, also make sure you modify the values in the `frontend-secrets.yaml` where https needs to be used instead of http.

> This is a basic K8s deployment of Skyvern and can be successfully used to create and run workflows. Further improvements may be made considering the use of the ports 9222 and 9090, improving the deployment scripts and integrating it with existing ones, etc.